"""timetable_edit 的增删改回归测试。

覆盖课程 / 考试 / 覆写三层的新增、修改、删除，以及若干必须被拦下的非法输入。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

import timetable_edit as edit
import timetable_service as ts


EXAMPLE = Path(__file__).resolve().parent.parent / "dev" / "course.json"


def make_store(tmp_path: Path) -> tuple[ts.TimetableStore, str, dict]:
    store = ts.TimetableStore(tmp_path)
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["courses"] = data["courses"][:2]
    data["exams"] = []
    data["overrides"] = []
    store.import_json("u", data)
    return store, "u", store.load("u")


# ---------------------------------------------------------------- 课程层


def test_add_course_generates_id_and_teachers(tmp_path: Path):
    store, uid, data = make_store(tmp_path)
    course = edit.add_course(data, {"name": "测试课", "teachers": ["张三", "李四"]})
    store.save(uid, data)
    assert course["id"] == "c1"
    assert [t["name"] for t in course["teachers"]] == ["张三", "李四"]
    assert store.load(uid)["courses"][-1]["name"] == "测试课"


def test_add_course_rejects_duplicate_id_and_missing_teacher(tmp_path: Path):
    store, uid, data = make_store(tmp_path)
    edit.add_course(data, {"id": "dup", "name": "甲", "teachers": ["张三"]})
    with pytest.raises(ts.TimetableError):
        edit.add_course(data, {"id": "dup", "name": "乙", "teachers": ["李四"]})
    with pytest.raises(ts.TimetableError):
        edit.add_course(data, {"name": "无教师"})


def test_find_course_prefers_exact_name(tmp_path: Path):
    _, _, data = make_store(tmp_path)
    edit.add_course(data, {"name": "程序设计基础", "teachers": ["张三"]})
    edit.add_course(data, {"name": "程序设计基础综合课程设计", "teachers": ["李四"]})
    course, _ = edit.find_course(data, "程序设计基础")
    assert course["name"] == "程序设计基础"


def test_find_course_reports_ambiguity(tmp_path: Path):
    _, _, data = make_store(tmp_path)
    edit.add_course(data, {"name": "算法基础", "teachers": ["张三"]})
    edit.add_course(data, {"name": "算法进阶", "teachers": ["李四"]})
    with pytest.raises(ts.TimetableError, match="多门课程"):
        edit.find_course(data, "算法")


def test_update_course_keeps_teacher_index(tmp_path: Path):
    store, uid, data = make_store(tmp_path)
    course = edit.add_course(data, {"name": "改名课", "teachers": ["张三", "李四"]})
    course["sessions"].append(
        {
            "id": "s1",
            "weeks": [1, 2],
            "day": 2,
            "periods": [3, 4],
            "location": {"raw": "测试楼101"},
            "teachers": ["T1"],
        }
    )
    store.save(uid, data)

    data = store.load(uid)
    edit.update_course(data, "c1", {"name": "改名后", "teachers": ["张三"]})
    store.save(uid, data)

    saved = store.load(uid)["courses"][-1]
    assert saved["name"] == "改名后"
    assert saved["teachers"] == [{"index": "T1", "name": "张三"}]
    assert saved["sessions"][0]["teachers"] == ["T1"]


def test_update_course_refuses_dropping_teacher_in_use(tmp_path: Path):
    _, _, data = make_store(tmp_path)
    course = edit.add_course(data, {"name": "课", "teachers": ["张三"]})
    course["sessions"].append(
        {
            "id": "s1",
            "weeks": [1],
            "day": 1,
            "periods": [1],
            "location": {"raw": "A101"},
            "teachers": ["T1"],
        }
    )
    with pytest.raises(ts.TimetableError):
        edit.update_course(data, "c1", {"teachers": ["王五"]})


def test_update_course_rejects_sessions_patch(tmp_path: Path):
    _, _, data = make_store(tmp_path)
    edit.add_course(data, {"name": "课", "teachers": ["张三"]})
    with pytest.raises(ts.TimetableError, match="排课"):
        edit.update_course(data, "c1", {"sessions": []})


def test_delete_course_removes_sessions(tmp_path: Path):
    store, uid, data = make_store(tmp_path)
    course = edit.add_course(data, {"name": "待删课", "teachers": ["张三"]})
    course["sessions"].append(
        {
            "id": "s1",
            "weeks": [1],
            "day": 1,
            "periods": [1],
            "location": {"raw": "A101"},
            "teachers": ["T1"],
        }
    )
    store.save(uid, data)
    store.set_reminder(uid, "c1", "s1", 30)

    data = store.load(uid)
    removed, refs = edit.delete_course(data, "c1")
    store.save(uid, data)
    assert removed["name"] == "待删课"
    assert refs == [("c1", "s1")]

    for course_id, session_id in refs:
        store.delete_reminder(uid, course_id, session_id)
    assert store.list_reminders(uid) == []
    assert all(c["id"] != "c1" for c in store.load(uid)["courses"])


# ---------------------------------------------------------------- 考试层


def test_exam_crud(tmp_path: Path):
    store, uid, data = make_store(tmp_path)
    exam = edit.add_exam(
        data,
        {"course": "程序设计基础", "time": "2027-01-09", "start_at": "14:00", "end_at": "17:00", "location": "J201"},
    )
    store.save(uid, data)
    assert exam["id"] == "e1"
    assert exam["location"]["raw"] == "J201"

    data = store.load(uid)
    found, _ = edit.find_exam(data, "2027-01-09")
    assert found["id"] == "e1"

    edit.update_exam(data, "e1", {"start_at": "15:00", "type": "补考"})
    store.save(uid, data)
    saved = store.load(uid)["exams"][0]
    assert saved["start_at"] == "15:00"
    assert saved["type"] == "补考"

    data = store.load(uid)
    edit.delete_exam(data, "e1")
    store.save(uid, data)
    assert store.load(uid)["exams"] == []


def test_exam_same_day_multiple_requires_id(tmp_path: Path):
    _, _, data = make_store(tmp_path)
    edit.add_exam(data, {"course": "科目A", "time": "2027-01-09", "start_at": "09:00"})
    edit.add_exam(data, {"course": "科目B", "time": "2027-01-09", "start_at": "14:00"})
    with pytest.raises(ts.TimetableError, match="多场考试"):
        edit.find_exam(data, "2027-01-09")
    exam, _ = edit.find_exam(data, "e2")
    assert exam["course"] == "科目B"


def test_exam_rejects_bad_date(tmp_path: Path):
    _, _, data = make_store(tmp_path)
    with pytest.raises(ts.TimetableError):
        edit.add_exam(data, {"course": "x", "time": "不是日期"})


# ---------------------------------------------------------------- 覆写层


def _first_ref(data: dict) -> str:
    course = data["courses"][0]
    return f'{course["id"]}:{course["sessions"][0]["id"]}'


def test_override_cancel_move_add(tmp_path: Path):
    store, uid, data = make_store(tmp_path)
    ref = _first_ref(data)

    cancel = edit.add_override(data, {"session_ref": ref, "type": "cancel", "date": "2026-11-20"})
    assert cancel["type"] == "cancel"
    move = edit.add_override(
        data,
        {"session_ref": ref, "type": "move", "date": "2026-11-21", "periods": [5, 6], "location": "备用楼"},
    )
    store.save(uid, data)
    assert move["periods"] == [5, 6]

    data = store.load(uid)
    edit.update_override(data, "o2", {"location": "备用楼303"})
    store.save(uid, data)
    assert store.load(uid)["overrides"][1]["location"] == "备用楼303"

    data = store.load(uid)
    agenda = ts.format_agenda(data, datetime.date(2026, 11, 20))
    assert "没有课程" in agenda
    moved = ts.format_agenda(data, datetime.date(2026, 11, 21))
    assert "备用楼303" in moved

    data = store.load(uid)
    edit.delete_override(data, "o1")
    store.save(uid, data)
    assert len(store.load(uid)["overrides"]) == 1


def test_override_accepts_positional_key(tmp_path: Path):
    _, _, data = make_store(tmp_path)
    ref = _first_ref(data)
    edit.add_override(data, {"session_ref": ref, "type": "cancel", "date": "2026-11-20"})
    override, index = edit.find_override(data, "1")
    assert index == 0 and override["type"] == "cancel"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "cancel", "date": "2026-11-20"},
        {"session_ref": "不存在:s1", "type": "cancel", "date": "2026-11-20"},
        {"session_ref": "@ref", "type": "move"},
        {"session_ref": "@ref", "type": "move", "date": "2026-11-20", "periods": [99]},
        {"session_ref": "@ref", "type": "未知类型", "date": "2026-11-20"},
    ],
)
def test_override_rejects_invalid(tmp_path: Path, payload: dict):
    _, _, data = make_store(tmp_path)
    ref = _first_ref(data)
    payload = {k: (ref if v == "@ref" else v) for k, v in payload.items()}
    with pytest.raises(ts.TimetableError):
        edit.add_override(data, payload)


def test_list_overrides_reports_human_readable(tmp_path: Path):
    _, _, data = make_store(tmp_path)
    assert edit.list_overrides(data) == []
    edit.add_override(data, {"session_ref": _first_ref(data), "type": "cancel", "date": "2026-11-20"})
    lines = edit.list_overrides(data)
    assert len(lines) == 1 and "停课" in lines[0] and "2026-11-20" in lines[0]


def test_compress_weeks():
    assert edit.compress_weeks([1, 2, 3, 5, 7, 8]) == "1-3,5,7-8"
    assert edit.compress_weeks([4]) == "4"
    assert edit.compress_weeks([]) == "无"
