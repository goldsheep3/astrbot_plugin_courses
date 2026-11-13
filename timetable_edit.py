"""课程表结构化编辑辅助。

这里只负责对**已通过校验的课表 dict**做原地修改与人类可读渲染，
落盘、加锁、校验统一交给 ``TimetableStore.load/save``。

设计约定：
* 每个 ``find_*`` 返回 ``(对象, 下标)``，找不到抛 ``TimetableError``。
* 每个 ``add_*`` / ``update_*`` / ``delete_*`` 只改 dict，不碰磁盘。
* 自动生成的 id 保证不与既有条目冲突。
"""

from __future__ import annotations

import copy
from typing import Any

try:  # AstrBot 以 data.plugins.<dir>.main 的形式按子包导入
    from .timetable_service import TimetableError, _parse_date, _parse_time
except ImportError:  # 直接以脚本/测试方式运行时
    from timetable_service import TimetableError, _parse_date, _parse_time


WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

OVERRIDE_TYPES = ("cancel", "move", "add")


# ---------------------------------------------------------------- 通用工具


def _text(value: Any) -> str:
    return str(value or "").strip()


def _location_raw(location: Any) -> str:
    if isinstance(location, dict):
        return _text(location.get("raw"))
    return _text(location)


def _make_location(raw: str, base: Any = None) -> dict[str, Any]:
    """在保留原有 campus/building/room 的前提下更新 raw。"""
    raw = _text(raw)
    if isinstance(base, dict):
        merged = copy.deepcopy(base)
        merged["raw"] = raw
        return merged
    return {"raw": raw}


def compress_weeks(weeks: list[int]) -> str:
    """把 [1,2,3,5,7,8] 压缩成 '1-3,5,7-8'。"""
    numbers = sorted({int(w) for w in weeks if isinstance(w, int)})
    if not numbers:
        return "无"
    spans: list[str] = []
    start = prev = numbers[0]
    for number in numbers[1:]:
        if number == prev + 1:
            prev = number
            continue
        spans.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = number
    spans.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(spans)


def _unique_id(existing: set[str], prefix: str) -> str:
    index = 1
    while f"{prefix}{index}" in existing:
        index += 1
    return f"{prefix}{index}"


def _course_ids(data: dict[str, Any]) -> set[str]:
    return {_text(c.get("id")) for c in data.get("courses", []) if _text(c.get("id"))}


def _period_indexes(data: dict[str, Any]) -> set[int]:
    return {p["index"] for p in data.get("periods", []) if isinstance(p.get("index"), int)}


# ---------------------------------------------------------------- 课程层


def find_course(data: dict[str, Any], key: str) -> tuple[dict[str, Any], int]:
    """按 id 精确匹配，其次按名称包含匹配（忽略大小写）。"""
    key = _text(key)
    if not key:
        raise TimetableError("课程标识不能为空")
    courses = data.get("courses", [])
    for index, course in enumerate(courses):
        if _text(course.get("id")) == key:
            return course, index
    exact = [(course, index) for index, course in enumerate(courses) if _text(course.get("name")) == key]
    if len(exact) == 1:
        return exact[0]
    lowered = key.lower()
    matched = [
        (course, index)
        for index, course in enumerate(courses)
        if lowered in _text(course.get("name")).lower() or lowered in _text(course.get("id")).lower()
    ]
    if not matched:
        raise TimetableError(f"找不到课程「{key}」")
    if len(matched) > 1:
        names = "、".join(f"{c.get('name')}({c.get('id')})" for c, _ in matched)
        raise TimetableError(f"「{key}」匹配到多门课程，请用 id 指定：{names}")
    return matched[0]


def _normalize_teachers(raw: Any, existing: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """把教师写法统一成 [{'index':..., 'name':...}]。

    支持三种输入：字符串名字、{'name':...}、完整 {'index':..., 'name':...}。
    """
    if not raw:
        return copy.deepcopy(existing or [])
    teachers: list[dict[str, Any]] = []
    existing = [t for t in (existing or []) if isinstance(t, dict)]
    # 同名教师沿用原 index，否则排课里的 teachers 引用会被判成悬空而清空
    by_name = {_text(t.get("name")): _text(t.get("index")) for t in existing if _text(t.get("name"))}
    used = {_text(t.get("index")) for t in existing}
    for item in raw if isinstance(raw, list) else [raw]:
        if isinstance(item, dict):
            name = _text(item.get("name"))
            index = _text(item.get("index"))
        else:
            name, index = _text(item), ""
        if not name and not index:
            continue
        if not index:
            index = by_name.get(name) or _unique_id(used, "T")
        used.add(index)
        teachers.append({"index": index, "name": name or index})
    return teachers


def add_course(data: dict[str, Any], course: Any) -> dict[str, Any]:
    """新增一门课程；``sessions`` 可省略，稍后用排课工具追加。"""
    if not isinstance(course, dict):
        raise TimetableError("课程必须是一个对象")
    name = _text(course.get("name"))
    if not name:
        raise TimetableError("课程缺少 name")
    course = copy.deepcopy(course)
    course["name"] = name
    course_id = _text(course.get("id"))
    if not course_id:
        course_id = _unique_id(_course_ids(data), "c")
    elif course_id in _course_ids(data):
        raise TimetableError(f"课程 id「{course_id}」已存在")
    course["id"] = course_id
    course["teachers"] = _normalize_teachers(course.get("teachers"))
    if not course["teachers"]:
        raise TimetableError("课程至少要有一位教师（teachers）")
    course["sessions"] = course.get("sessions") or []
    data.setdefault("courses", []).append(course)
    return course


def update_course(data: dict[str, Any], key: str, patch: Any) -> dict[str, Any]:
    """更新课程名称/教师/id 等元信息；排课请用 session 工具。"""
    if not isinstance(patch, dict) or not patch:
        raise TimetableError("没有提供要修改的字段")
    course, _ = find_course(data, key)
    if "sessions" in patch:
        raise TimetableError("排课请用 timetable_add_session / update_session / delete_session 修改")
    if "name" in patch:
        name = _text(patch["name"])
        if not name:
            raise TimetableError("课程名称不能为空")
        course["name"] = name
    if "teachers" in patch:
        teachers = _normalize_teachers(patch["teachers"], course.get("teachers"))
        if not teachers:
            raise TimetableError("课程至少要保留一位教师")
        allowed = {t["index"] for t in teachers}
        for session in course.get("sessions", []):
            kept = [t for t in session.get("teachers", []) if t in allowed]
            if not kept:
                raise TimetableError(f"排课 {session.get('id')} 引用的教师会被清空，请先改排课")
            session["teachers"] = kept
        course["teachers"] = teachers
    if "id" in patch:
        new_id = _text(patch["id"])
        if not new_id:
            raise TimetableError("课程 id 不能为空")
        if new_id != course["id"] and new_id in _course_ids(data):
            raise TimetableError(f"课程 id「{new_id}」已存在")
        course["id"] = new_id
    return course


def delete_course(data: dict[str, Any], key: str) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """删除课程及其全部排课，返回 (课程, [(course_id, session_id), ...])。"""
    course, _ = find_course(data, key)
    refs = [(_text(course.get("id")), _text(s.get("id"))) for s in course.get("sessions", [])]
    data["courses"] = [c for c in data.get("courses", []) if c is not course]
    return course, refs


def describe_course(data: dict[str, Any], course: dict[str, Any]) -> str:
    teachers = "、".join(_text(t.get("name")) or _text(t.get("index")) for t in course.get("teachers", []))
    lines = [f"{course.get('name')}（id {course.get('id')}）", f"教师：{teachers or '未填'}"]
    sessions = course.get("sessions", [])
    if not sessions:
        lines.append("排课：暂无")
        return "\n".join(lines)
    lines.append(f"排课：{len(sessions)} 段")
    for session in sessions:
        day = session.get("day")
        weekday = WEEKDAYS[day - 1] if isinstance(day, int) and 1 <= day <= 7 else f"第{day}天"
        periods = session.get("periods", [])
        span = f"{periods[0]}-{periods[-1]}" if len(periods) > 1 else (str(periods[0]) if periods else "?")
        lines.append(
            f"  {session.get('id')}：第{compress_weeks(session.get('weeks', []))}周 {weekday} "
            f"第{span}节｜{_location_raw(session.get('location')) or '地点待定'}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- 考试层


def _exam_ids(data: dict[str, Any]) -> set[str]:
    return {_text(e.get("id")) for e in data.get("exams", []) if isinstance(e, dict) and _text(e.get("id"))}


def find_exam(data: dict[str, Any], key: str) -> tuple[dict[str, Any], int]:
    """按 id 精确匹配，其次按日期，再按课程名包含匹配。"""
    key = _text(key)
    if not key:
        raise TimetableError("考试标识不能为空")
    exams = [e for e in data.get("exams", []) if isinstance(e, dict)]
    for index, exam in enumerate(exams):
        if _text(exam.get("id")) == key:
            return exam, index
    try:
        target = _parse_date(key)
    except TimetableError:
        target = None
    if target is not None:
        matched = [(e, i) for i, e in enumerate(exams) if _text(e.get("time") or e.get("date")) == target.isoformat()]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            names = "、".join(f"{e.get('course') or e.get('name')}({e.get('id')})" for e, _ in matched)
            raise TimetableError(f"{target.isoformat()} 有多场考试，请用 id 指定：{names}")
        raise TimetableError(f"{target.isoformat()} 没有考试安排")
    lowered = key.lower()
    matched = [
        (exam, index)
        for index, exam in enumerate(exams)
        if lowered in _text(exam.get("course")).lower() or lowered in _text(exam.get("name")).lower()
    ]
    if not matched:
        raise TimetableError(f"找不到考试「{key}」")
    if len(matched) > 1:
        names = "、".join(f"{e.get('course')}({e.get('id')})" for e, _ in matched)
        raise TimetableError(f"「{key}」匹配到多场考试，请用 id 指定：{names}")
    return matched[0]


def _normalize_exam(data: dict[str, Any], exam: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(exam, dict):
        raise TimetableError("考试必须是一个对象")
    merged = copy.deepcopy(base or {})
    merged.update(copy.deepcopy(exam))
    if "time" in merged:
        merged["time"] = _parse_date(_text(merged["time"])).isoformat()
    elif not merged.get("time"):
        raise TimetableError("考试缺少 time（YYYY-MM-DD）")
    for field in ("start_at", "end_at"):
        if merged.get(field):
            merged[field] = _parse_time(_text(merged[field])).strftime("%H:%M")
    location = merged.get("location")
    if isinstance(location, str):
        merged["location"] = {"raw": _text(location)}
    elif isinstance(location, dict):
        merged["location"] = _make_location(_location_raw(location), location)
    if not _text(merged.get("course")) and not _text(merged.get("name")):
        raise TimetableError("考试缺少 course（科目）")
    if not _text(merged.get("type")):
        merged["type"] = "考试"
    exam_id = _text(merged.get("id"))
    if not exam_id:
        exam_id = _unique_id(_exam_ids(data), "e")
    merged["id"] = exam_id
    return merged


def add_exam(data: dict[str, Any], exam: Any) -> dict[str, Any]:
    normalized = _normalize_exam(data, exam)
    if normalized["id"] in _exam_ids(data):
        raise TimetableError(f"考试 id「{normalized['id']}」已存在")
    data.setdefault("exams", []).append(normalized)
    return normalized


def update_exam(data: dict[str, Any], key: str, patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise TimetableError("没有提供要修改的字段")
    exam, index = find_exam(data, key)
    original_id = _text(exam.get("id"))
    merged = _normalize_exam(data, patch, exam)
    if merged["id"] != original_id and merged["id"] in _exam_ids(data):
        raise TimetableError(f"考试 id「{merged['id']}」已存在")
    data["exams"][index] = merged
    return merged


def delete_exam(data: dict[str, Any], key: str) -> dict[str, Any]:
    exam, _ = find_exam(data, key)
    data["exams"] = [e for e in data.get("exams", []) if e is not exam]
    return exam


def describe_exam(exam: dict[str, Any]) -> str:
    place = _location_raw(exam.get("location")) or "地点待定"
    span = ""
    if exam.get("start_at") or exam.get("end_at"):
        span = f"{exam.get('start_at', '?')}-{exam.get('end_at', '?')} "
    return (
        f"{exam.get('time', '?')} {span}"
        f"{_text(exam.get('course')) or _text(exam.get('name'))}（{exam.get('type', '考试')}）"
        f"｜{place}｜id {exam.get('id', '?')}"
    )


# ---------------------------------------------------------------- 覆写层


def _override_ids(data: dict[str, Any]) -> set[str]:
    return {_text(o.get("id")) for o in data.get("overrides", []) if isinstance(o, dict) and _text(o.get("id"))}


def _resolve_ref(data: dict[str, Any], ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
    course_id, _, session_id = _text(ref).partition(":")
    if not course_id or not session_id:
        raise TimetableError("session_ref 应为「课程id:排课id」")
    for course in data.get("courses", []):
        if _text(course.get("id")) != course_id:
            continue
        for session in course.get("sessions", []):
            if _text(session.get("id")) == session_id:
                return course, session
        raise TimetableError(f"课程 {course_id} 没有排课 {session_id}")
    raise TimetableError(f"找不到课程 {course_id}")


def find_override(data: dict[str, Any], key: str) -> tuple[dict[str, Any], int]:
    """按 id 精确匹配；也接受 1 起的序号（对应 list_overrides 的输出顺序）。"""
    key = _text(key)
    overrides = [o for o in data.get("overrides", []) if isinstance(o, dict)]
    for index, override in enumerate(overrides):
        if _text(override.get("id")) == key:
            return override, index
    if key.isdigit():
        position = int(key) - 1
        if 0 <= position < len(overrides):
            return overrides[position], position
        raise TimetableError(f"序号 {key} 超出范围（共 {len(overrides)} 条覆写）")
    raise TimetableError(f"找不到覆写「{key}」")


def _normalize_override(data: dict[str, Any], override: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(override, dict):
        raise TimetableError("覆写必须是一个对象")
    merged = copy.deepcopy(base or {})
    merged.update(copy.deepcopy(override))

    kind = _text(merged.get("type") or merged.get("kind") or merged.get("action")).lower()
    aliases = {"取消": "cancel", "停课": "cancel", "调课": "move", "改期": "move", "补课": "add", "加课": "add"}
    kind = aliases.get(kind, kind)
    if kind not in OVERRIDE_TYPES:
        raise TimetableError("type 只能是 cancel（停课）/ move（调课）/ add（补课）")
    merged["type"] = kind

    ref = _text(merged.get("session_ref") or merged.get("session"))
    if not ref:
        raise TimetableError("缺少 session_ref（课程id:排课id）")
    _resolve_ref(data, ref)
    merged["session_ref"] = ref

    raw_date = merged.get("date") or merged.get("on")
    if raw_date:
        merged["date"] = _parse_date(_text(raw_date)).isoformat()
    elif kind in ("move", "add"):
        raise TimetableError(f"{kind} 类型的覆写必须指定 date（YYYY-MM-DD）")

    raw_from = merged.pop("from", None) or merged.get("from_date")
    if raw_from:
        if kind != "move":
            raise TimetableError("只有 move（调课）支持 from_date")
        merged["from_date"] = _parse_date(_text(raw_from)).isoformat()
        if merged.get("date") and merged["from_date"] == merged["date"]:
            raise TimetableError("from_date 与 date 不能是同一天")
    else:
        merged.pop("from_date", None)

    if merged.get("periods") is not None:
        periods = merged["periods"]
        if not isinstance(periods, list) or not periods:
            raise TimetableError("periods 必须是非空数组")
        valid = _period_indexes(data)
        for item in periods:
            if not isinstance(item, int) or item not in valid:
                raise TimetableError(f"节次 {item} 不存在")
    if merged.get("location") is not None:
        merged["location"] = _location_raw(merged["location"])

    override_id = _text(merged.get("id"))
    if not override_id:
        override_id = _unique_id(_override_ids(data), "o")
    merged["id"] = override_id
    return merged


def add_override(data: dict[str, Any], override: Any) -> dict[str, Any]:
    normalized = _normalize_override(data, override)
    if normalized["id"] in _override_ids(data):
        raise TimetableError(f"覆写 id「{normalized['id']}」已存在")
    data.setdefault("overrides", []).append(normalized)
    return normalized


def update_override(data: dict[str, Any], key: str, patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise TimetableError("没有提供要修改的字段")
    override, index = find_override(data, key)
    original_id = _text(override.get("id"))
    merged = _normalize_override(data, patch, override)
    if merged["id"] != original_id and merged["id"] in _override_ids(data):
        raise TimetableError(f"覆写 id「{merged['id']}」已存在")
    data["overrides"][index] = merged
    return merged


def delete_override(data: dict[str, Any], key: str) -> dict[str, Any]:
    override, _ = find_override(data, key)
    data["overrides"] = [o for o in data.get("overrides", []) if o is not override]
    return override


def describe_override(data: dict[str, Any], override: dict[str, Any], position: int | None = None) -> str:
    kind = _text(override.get("type")) or "?"
    label = {"cancel": "停课", "move": "调课", "add": "补课"}.get(kind, kind)
    ref = _text(override.get("session_ref"))
    try:
        course, session = _resolve_ref(data, ref)
        name = f"{course.get('name')}（{ref}）"
    except TimetableError:
        name = f"{ref}（课程已不存在）"
    when = _text(override.get("date")) or "每周"
    source = _text(override.get("from_date"))
    if source and kind == "move":
        when = f"{source} → {when}"
    parts = [f"{position}. [{label}] {when} {name}"]
    if override.get("periods"):
        parts.append(f"节次 {override['periods']}")
    if override.get("location"):
        parts.append(f"地点 {_location_raw(override['location'])}")
    parts.append(f"id {override.get('id', '?')}")
    return "｜".join(parts)


def list_overrides(data: dict[str, Any]) -> list[str]:
    overrides = [o for o in data.get("overrides", []) if isinstance(o, dict)]
    if not overrides:
        return []
    return [describe_override(data, o, i + 1) for i, o in enumerate(overrides)]


def session_refs_of(course: dict[str, Any]) -> list[str]:
    """课程的排课引用，用于清理关联提醒。"""
    course_id = _text(course.get("id"))
    return [f"{course_id}:{_text(s.get('id'))}" for s in course.get("sessions", [])]
