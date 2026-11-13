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


OVERRIDE_CANCEL = {"cancel", "cancelled", "canceled", "off", "skip", "停课", "取消", "停"}
OVERRIDE_MOVE = {"move", "reschedule", "change", "shift", "调课", "调整", "换教室", "改期"}
OVERRIDE_ADD = {"add", "extra", "makeup", "补课", "加课"}


def _override_kind(override: dict[str, Any]) -> str:
    """Classify an override entry.

    ``dev/schema.json`` leaves ``override`` open (only ``session_ref`` is
    pinned), so the parser accepts several spellings and falls back to
    ``unknown`` instead of guessing wrongly.  Unknown entries are ignored by
    ``occurrences_on`` so a half-designed override can never corrupt the
    timetable view.
    """
    raw = override.get("type") or override.get("kind") or override.get("action") or ""
    kind = str(raw).strip().lower()
    if kind in OVERRIDE_CANCEL or override.get("cancelled") or override.get("cancel"):
        return "cancel"
    if kind in OVERRIDE_ADD or override.get("add") or override.get("makeup"):
        return "add"
    if kind in OVERRIDE_MOVE:
        return "move"
    if any(key in override for key in ("date", "start", "end", "periods", "location", "day")):
        return "move"
    return "unknown"


def _override_date(override: dict[str, Any]) -> date | None:
    """Date an override targets, ``None`` when it applies to every week.

    Unparseable dates collapse to ``date.min`` so the entry simply never
    matches instead of taking the whole timetable down with it.
    """
    raw = override.get("date") or override.get("on") or override.get("day_date")
    if not raw:
        return None
    try:
        return _parse_date(str(raw))
    except TimetableError:
        return date.min


def _resolve_session(
    data: dict[str, Any], session_ref: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    course_id, _, session_id = str(session_ref).partition(":")
    for course in data["courses"]:
        if course["id"] != course_id:
            continue
        for session in course["sessions"]:
            if session["id"] == session_id:
                return course, session
    return None


def _build_event(
    course: dict[str, Any],
    session: dict[str, Any],
    target: date,
    periods: dict[int, dict[str, Any]],
    *,
    period_indexes: list[int] | None = None,
    location: str | None = None,
    week: int | None = None,
    source: str = "schedule",
) -> dict[str, Any]:
    teachers = {teacher["index"]: teacher["name"] for teacher in course["teachers"]}
    chosen = period_indexes if period_indexes is not None else session["periods"]
    session_periods = [periods[index] for index in sorted(chosen)]
    return {
        "course_id": course["id"],
        "session_id": session["id"],
        "course_name": course["name"],
        "week": week,
        "date": target.isoformat(),
        "start": min(p["start"] for p in session_periods),
        "end": max(p["end"] for p in session_periods),
        "periods": [p["index"] for p in session_periods],
        "location": location or session["location"]["raw"],
        "teachers": [teachers[teacher] for teacher in session["teachers"]],
        "source": source,
    }


def occurrences_on(data: dict[str, Any], target: date) -> list[dict[str, Any]]:
    """Expand recurring sessions into chronologically ordered events for a date.

    ``overrides`` are applied afterwards: cancelled entries are dropped, moved
    entries shift date/period/location, and extra entries are appended.
    """
    validate_timetable(data)
    first_monday = _parse_date(data["calendar"]["first_monday"])
    delta_days = (target - first_monday).days
    week, weekday = divmod(delta_days, 7)
    week += 1
    periods = {p["index"]: p for p in data["periods"]}
    result: list[dict[str, Any]] = []
    if 1 <= week <= data["calendar"]["week_count"]:
        for course in data["courses"]:
            for session in course["sessions"]:
                if week not in session["weeks"] or session["day"] != weekday + 1:
                    continue
                result.append(_build_event(course, session, target, periods, week=week))
    result = _apply_overrides(data, result, target, periods, week)
    return sorted(result, key=lambda item: item["start"])


def _patch_event(
    event: dict[str, Any], override: dict[str, Any], periods: dict[int, dict[str, Any]]
) -> None:
    """Apply a same-day move: shift times, periods or location in place."""
    for key in ("start", "end"):
        value = override.get(key)
        if isinstance(value, str) and value:
            event[key] = value
    location = override.get("location")
    if isinstance(location, str) and location:
        event["location"] = location
    elif isinstance(location, dict) and location.get("raw"):
        event["location"] = location["raw"]
    if isinstance(override.get("periods"), list) and override["periods"]:
        indexes = [int(i) for i in override["periods"] if int(i) in periods]
        if indexes:
            event["periods"] = sorted(indexes)
            event["start"] = min(periods[i]["start"] for i in indexes)
            event["end"] = max(periods[i]["end"] for i in indexes)
    event["source"] = "override"


def _apply_overrides(
    data: dict[str, Any],
    events: list[dict[str, Any]],
    target: date,
    periods: dict[int, dict[str, Any]],
    week: int,
) -> list[dict[str, Any]]:
    overrides = data.get("overrides") or []
    if not overrides:
        return events

    by_ref: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_ref.setdefault(f"{event['course_id']}:{event['session_id']}", []).append(event)

    kept: list[dict[str, Any]] = []
    dropped_refs: set[str] = set()
    for event in events:
        ref = f"{event['course_id']}:{event['session_id']}"
        for override in overrides:
            if str(override.get("session_ref", "")) != ref:
                continue
            kind = _override_kind(override)
            moved_to = _override_date(override)
            if kind == "cancel":
                if moved_to is None or moved_to == target:
                    dropped_refs.add(ref)
                    break
                continue
            if kind == "move":
                if moved_to is not None and moved_to != target:
                    # Moved away from this date; it reappears on ``moved_to``.
                    dropped_refs.add(ref)
                    break
                _patch_event(event, override, periods)
                break
        if ref not in dropped_refs:
            kept.append(event)

    for override in overrides:
        kind = _override_kind(override)
        if kind not in ("move", "add"):
            continue
        if _override_date(override) != target:
            continue
        ref = str(override.get("session_ref", ""))
        if ref in by_ref:
            # The session already runs on this date, so it was patched above.
            continue
        resolved = _resolve_session(data, ref)
        if resolved is None:
            continue
        course, session = resolved
        indexes = override.get("periods") or session["periods"]
        indexes = [int(i) for i in indexes if int(i) in periods]
        if not indexes:
            continue
        location = override.get("location") or session["location"]["raw"]
        kept.append(
            _build_event(
                course,
                session,
                target,
                periods,
                period_indexes=indexes,
                location=location if isinstance(location, str) else session["location"]["raw"],
                week=week if week >= 1 else None,
                source="override",
            )
        )
    return kept


def exams_on(data: dict[str, Any], target: date) -> list[dict[str, Any]]:
    """Return exams scheduled on ``target``, earliest first."""
    result: list[dict[str, Any]] = []
    for exam in data.get("exams") or []:
        if not isinstance(exam, dict):
            continue
        try:
            exam_date = _parse_date(str(exam.get("time") or exam.get("date")))
        except TimetableError:
            continue
        if exam_date == target:
            result.append(exam)
    return sorted(result, key=lambda item: str(item.get("start_at", "")))


def format_exams(data: dict[str, Any], target: date) -> str:
    exams = exams_on(data, target)
    if not exams:
        return f"{target.isoformat()} 没有考试安排。"
    lines = [f"{target.isoformat()} 考试："]
    for exam in exams:
        location = exam.get("location") or {}
        place = location.get("raw") if isinstance(location, dict) else str(location)
        lines.append(
            f"{exam.get('start_at', '?')}-{exam.get('end_at', '?')} "
            f"{exam.get('course', '?')}（{exam.get('type', '考试')}）｜{place or '地点待定'}"
        )
    return "\n".join(lines)

def format_agenda(data: dict[str, Any], target: date) -> str:
    events = occurrences_on(data, target)
    header = f"{target.isoformat()}（第{((target - _parse_date(data['calendar']['first_monday'])).days // 7) + 1}周，周{WEEKDAY_NAMES[target.weekday()]}）"
    exams = exams_on(data, target)
    exam_lines = []
    for exam in exams:
        location = exam.get("location") or {}
        place = location.get("raw") if isinstance(location, dict) else str(location)
        exam_lines.append(
            f"【{exam.get('type', '考试')}】{exam.get('course', '?')} "
            f"{exam.get('start_at', '?')}-{exam.get('end_at', '?')}｜{place or '地点待定'}"
        )
    if not events:
        return "\n".join([f"{header}\n当天没有课程。", *exam_lines])
    lines = [header, *exam_lines]
    for event in events:
        lines.append(
            f"{event['start']}-{event['end']} 第{','.join(map(str, event['periods']))}节 "
            f"{event['course_name']}｜{event['location']}｜{('、'.join(event['teachers']))}"
            + ("（调课）" if event.get("source") == "override" else "")
        )
    return "\n".join(lines)


@dataclass
class TimetableStore:
    root: Path
    example_path: Path | None = None

    @staticmethod
    def normalize(user_id: str) -> str:
        """Map a platform user id to the key used for every file on disk."""
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(user_id))
        return safe_id or "anonymous"

    def _path(self, user_id: str) -> Path:
        # User ids come from the platform; still make the filename safe.
        return self.root / f"{self.normalize(user_id)}.json"

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


    def _sessions_path(self) -> Path:
        return self.root / "sessions.json"

    def _sent_path(self) -> Path:
        return self.root / "sent_reminders.json"

    def bind_session(self, user_id: str, session_ref: str) -> None:
        """Remember where to push reminders for a user (last active chat wins)."""
        if not session_ref:
            return
        path = self._sessions_path()
        mapping: dict[str, str] = {}
        if path.exists():
            try:
                mapping = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                mapping = {}
        user_id = self.normalize(user_id)
        if mapping.get(user_id) == session_ref:
            return
        mapping[user_id] = session_ref
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")

    def session_of(self, user_id: str) -> str | None:
        path = self._sessions_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get(self.normalize(user_id))
        except (json.JSONDecodeError, AttributeError):
            return None

    def users(self) -> list[str]:
        """Every user that has a timetable file on disk."""
        if not self.root.exists():
            return []
        reserved = {"sessions.json", "settings.json", "sent_reminders.json"}
        return sorted(
            path.stem
            for path in self.root.glob("*.json")
            if path.name not in reserved and not path.name.endswith(".reminders.json")
        )

    def users_with_reminders(self) -> list[str]:
        """User ids that currently have a reminder file on disk."""
        if not self.root.exists():
            return []
        return sorted(
            path.name[: -len(".reminders.json")]
            for path in self.root.glob("*.reminders.json")
        )

    def _settings_path(self) -> Path:
        return self.root / "settings.json"

    def get_setting(self, key: str, default: Any = None) -> Any:
        path = self._settings_path()
        if not path.exists():
            return default
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
        return data.get(key, default) if isinstance(data, dict) else default

    def set_setting(self, key: str, value: Any) -> None:
        path = self._settings_path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except json.JSONDecodeError:
                data = {}
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def morning_report_time(self, user_id: str) -> time | None:
        """Daily agenda push time for one user, or None when disabled."""
        raw = self.get_setting(f"morning_report:{self.normalize(user_id)}")
        if not raw:
            return None
        try:
            return _parse_time(str(raw))
        except TimetableError:
            return None

    def sent_keys(self, now: datetime | None = None) -> set[str]:
        """Idempotency keys of reminders already delivered (kept for 7 days)."""
        path = self._sent_path()
        if not path.exists():
            return set()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return set()
        cutoff = ((now or datetime.now()) - timedelta(days=7)).isoformat(timespec="minutes")
        return {key for key, stamp in raw.items() if isinstance(stamp, str) and stamp >= cutoff}

    def mark_sent(self, keys: list[str], now: datetime | None = None) -> None:
        path = self._sent_path()
        raw: dict[str, str] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = {}
        now = now or datetime.now()
        stamp = now.isoformat(timespec="minutes")
        cutoff = (now - timedelta(days=7)).isoformat(timespec="minutes")
        raw = {key: value for key, value in raw.items() if isinstance(value, str) and value >= cutoff}
        for key in keys:
            raw[key] = stamp
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def pending_reminders(
    data: dict[str, Any],
    reminders: list[dict[str, Any]],
    now: datetime,
    sent_keys: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Return reminders that are due at ``now``.

    A reminder becomes due once its lead time is reached and stays due until the
    session starts; the caller owns the idempotency keys so a crash in the
    middle of a push can simply be retried.
    """
    enabled = [r for r in reminders if r.get("enabled", True)]
    if not enabled:
        return []
    due: list[dict[str, Any]] = []
    for target in (now.date(), now.date() + timedelta(days=1)):
        for event in occurrences_on(data, target):
            ref = f"{event['course_id']}:{event['session_id']}"
            start_dt = datetime.combine(target, _parse_time(event["start"]))
            for reminder in enabled:
                if reminder.get("session_ref") != ref:
                    continue
                minutes = int(reminder["minutes_before"])
                remind_at = start_dt - timedelta(minutes=minutes)
                key = f"{ref}@{target.isoformat()}"
                if remind_at <= now < start_dt and key not in sent_keys:
                    due.append({
                        **event,
                        "minutes_before": minutes,
                        "remind_at": remind_at.isoformat(timespec="minutes"),
                        "key": key,
                    })
    return due


def format_reminder(event: dict[str, Any]) -> str:
    """Render one due reminder as a short chat message."""
    minutes = event["minutes_before"]
    lead = "马上开始" if minutes == 0 else f"{minutes} 分钟后开始"
    return (
        f"提醒：{event['course_name']} {lead}\n"
        f"{event['start']}-{event['end']}｜{event['location']}"
    )
