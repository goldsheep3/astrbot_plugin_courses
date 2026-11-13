"""Storage and calendar operations for the timetable plugin.

This module deliberately has no AstrBot dependency.  It is consequently easy to
test and can also be used by the LLM tool handlers in ``main.py``.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any


WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")


class TimetableError(ValueError):
    """Raised when an edit would leave a timetable invalid."""


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise TimetableError("日期必须使用 YYYY-MM-DD 格式") from exc


def _parse_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise TimetableError("时间必须使用 HH:MM 格式") from exc


def validate_timetable(data: dict[str, Any]) -> None:
    """Validate the parts of schema.json that are essential for safe use.

    jsonschema is intentionally not required by the plugin runtime.
    """
    for key in ("id", "title", "calendar", "periods", "courses", "exams", "overrides"):
        if key not in data:
            raise TimetableError(f"缺少字段: {key}")
    calendar = data["calendar"]
    _parse_date(calendar.get("first_monday"))
    if not isinstance(calendar.get("week_count"), int) or calendar["week_count"] < 1:
        raise TimetableError("calendar.week_count 必须是正整数")

    period_indexes: set[int] = set()
    for period in data["periods"]:
        index = period.get("index")
        if not isinstance(index, int) or index < 1 or index in period_indexes:
            raise TimetableError("节次 index 必须是唯一正整数")
        _parse_time(period.get("start"))
        _parse_time(period.get("end"))
        period_indexes.add(index)

    course_ids: set[str] = set()
    for course in data["courses"]:
        course_id = course.get("id")
        if not isinstance(course_id, str) or not course_id or course_id in course_ids:
            raise TimetableError("课程 id 必须是唯一的非空字符串")
        course_ids.add(course_id)
        teacher_ids = {teacher.get("index") for teacher in course.get("teachers", [])}
        if not course.get("name") or not teacher_ids:
            raise TimetableError(f"课程 {course_id} 缺少名称或教师")
        session_ids: set[str] = set()
        for session in course.get("sessions", []):
            session_id = session.get("id")
            if not isinstance(session_id, str) or not session_id or session_id in session_ids:
                raise TimetableError(f"课程 {course_id} 的 session id 无效")
            session_ids.add(session_id)
            weeks = session.get("weeks")
            if not isinstance(weeks, list) or not weeks or any(not isinstance(w, int) or w < 1 or w > calendar["week_count"] for w in weeks):
                raise TimetableError(f"课程 {course_id}/{session_id} 的周次无效")
            if session.get("day") not in range(1, 8):
                raise TimetableError(f"课程 {course_id}/{session_id} 的星期无效")
            if not set(session.get("periods", [])) <= period_indexes:
                raise TimetableError(f"课程 {course_id}/{session_id} 引用了不存在的节次")
            if not session.get("location", {}).get("raw"):
                raise TimetableError(f"课程 {course_id}/{session_id} 缺少地点")
            if not set(session.get("teachers", [])) <= teacher_ids:
                raise TimetableError(f"课程 {course_id}/{session_id} 引用了不存在的教师")


def occurrences_on(data: dict[str, Any], target: date) -> list[dict[str, Any]]:
    """Expand recurring sessions into chronologically ordered events for a date."""
    validate_timetable(data)
    first_monday = _parse_date(data["calendar"]["first_monday"])
    delta_days = (target - first_monday).days
    week, weekday = divmod(delta_days, 7)
    week += 1
    if week < 1 or week > data["calendar"]["week_count"]:
        return []
    periods = {p["index"]: p for p in data["periods"]}
    result: list[dict[str, Any]] = []
    for course in data["courses"]:
        teachers = {teacher["index"]: teacher["name"] for teacher in course["teachers"]}
        for session in course["sessions"]:
            if week not in session["weeks"] or session["day"] != weekday + 1:
                continue
            session_periods = [periods[index] for index in sorted(session["periods"])]
            result.append({
                "course_id": course["id"],
                "session_id": session["id"],
                "course_name": course["name"],
                "week": week,
                "date": target.isoformat(),
                "start": min(p["start"] for p in session_periods),
                "end": max(p["end"] for p in session_periods),
                "periods": [p["index"] for p in session_periods],
                "location": session["location"]["raw"],
                "teachers": [teachers[teacher] for teacher in session["teachers"]],
            })
    return sorted(result, key=lambda item: item["start"])


def format_agenda(data: dict[str, Any], target: date) -> str:
    events = occurrences_on(data, target)
    header = f"{target.isoformat()}（第{((target - _parse_date(data['calendar']['first_monday'])).days // 7) + 1}周，周{WEEKDAY_NAMES[target.weekday()]}）"
    if not events:
        return f"{header}\n当天没有课程。"
    lines = [header]
    for event in events:
        lines.append(
            f"{event['start']}-{event['end']} 第{','.join(map(str, event['periods']))}节 "
            f"{event['course_name']}｜{event['location']}｜{('、'.join(event['teachers']))}"
        )
    return "\n".join(lines)


@dataclass
class TimetableStore:
    root: Path
    example_path: Path | None = None

    def _path(self, user_id: str) -> Path:
        # User ids come from the platform; still make the filename safe.
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return self.root / f"{safe_id or 'anonymous'}.json"

    def load(self, user_id: str) -> dict[str, Any]:
        path = self._path(user_id)
        if not path.exists():
            raise TimetableError("尚未导入课表。请先使用“课表 导入示例”或让 LLM 调用 timetable_import。")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TimetableError("已保存的课表 JSON 损坏") from exc
        validate_timetable(data)
        return data

    def save(self, user_id: str, data: dict[str, Any]) -> None:
        validate_timetable(data)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def import_json(self, user_id: str, timetable_json: str | dict[str, Any]) -> dict[str, Any]:
        try:
            data = json.loads(timetable_json) if isinstance(timetable_json, str) else copy.deepcopy(timetable_json)
        except json.JSONDecodeError as exc:
            raise TimetableError(f"课表 JSON 无法解析: {exc.msg}") from exc
        self.save(user_id, data)
        return data

    def import_example(self, user_id: str) -> dict[str, Any]:
        if self.example_path is None or not self.example_path.exists():
            raise TimetableError("插件未随附示例课表")
        return self.import_json(user_id, self.example_path.read_text(encoding="utf-8"))

    def replace_session(self, user_id: str, course_id: str, session_id: str, session: dict[str, Any]) -> None:
        data = self.load(user_id)
        for course in data["courses"]:
            if course["id"] == course_id:
                for index, old in enumerate(course["sessions"]):
                    if old["id"] == session_id:
                        session["id"] = session_id
                        course["sessions"][index] = session
                        self.save(user_id, data)
                        return
                raise TimetableError(f"课程 {course_id} 没有排课 {session_id}")
        raise TimetableError(f"找不到课程 {course_id}")

    def add_session(self, user_id: str, course_id: str, session: dict[str, Any]) -> None:
        data = self.load(user_id)
        for course in data["courses"]:
            if course["id"] == course_id:
                if any(old["id"] == session.get("id") for old in course["sessions"]):
                    raise TimetableError(f"课程 {course_id} 已有排课 {session.get('id')}")
                course["sessions"].append(session)
                self.save(user_id, data)
                return
        raise TimetableError(f"找不到课程 {course_id}")

    def delete_session(self, user_id: str, course_id: str, session_id: str) -> None:
        data = self.load(user_id)
        for course in data["courses"]:
            if course["id"] == course_id:
                if not any(s["id"] == session_id for s in course["sessions"]):
                    raise TimetableError(f"课程 {course_id} 没有排课 {session_id}")
                course["sessions"] = [s for s in course["sessions"] if s["id"] != session_id]
                if not course["sessions"]:
                    data["courses"] = [c for c in data["courses"] if c["id"] != course_id]
                self.save(user_id, data)
                return
        raise TimetableError(f"找不到课程 {course_id}")

    def _reminder_path(self, user_id: str) -> Path:
        return self._path(user_id).with_suffix(".reminders.json")

    def list_reminders(self, user_id: str) -> list[dict[str, Any]]:
        path = self._reminder_path(user_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TimetableError("已保存的提醒 JSON 损坏") from exc
        if not isinstance(data, list):
            raise TimetableError("已保存的提醒格式无效")
        return data

    def set_reminder(self, user_id: str, course_id: str, session_id: str, minutes_before: int) -> None:
        if not isinstance(minutes_before, int) or not 0 <= minutes_before <= 24 * 60:
            raise TimetableError("提前提醒分钟数必须在 0 至 1440 之间")
        data = self.load(user_id)
        found = any(course["id"] == course_id and any(s["id"] == session_id for s in course["sessions"]) for course in data["courses"])
        if not found:
            raise TimetableError("提醒的课程或排课不存在")
        reminders = [r for r in self.list_reminders(user_id) if r.get("session_ref") != f"{course_id}:{session_id}"]
        reminders.append({"session_ref": f"{course_id}:{session_id}", "minutes_before": minutes_before, "enabled": True})
        self.root.mkdir(parents=True, exist_ok=True)
        self._reminder_path(user_id).write_text(json.dumps(reminders, ensure_ascii=False, indent=2), encoding="utf-8")

    def delete_reminder(self, user_id: str, course_id: str, session_id: str) -> bool:
        before = self.list_reminders(user_id)
        after = [r for r in before if r.get("session_ref") != f"{course_id}:{session_id}"]
        if len(before) == len(after):
            return False
        self._reminder_path(user_id).write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
