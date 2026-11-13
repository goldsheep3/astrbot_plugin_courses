import copy
import json
from datetime import date, time
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from timetable_service import (
    TimetableStore,
    exams_on,
    format_exams,
    occurrences_on,
)

EXAMPLE = PROJECT_ROOT / "dev" / "course.json"
MONDAY = date(2026, 8, 31)
TUESDAY = date(2026, 9, 1)


@pytest.fixture
def data():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _monday_ref(data):
    """第一周周一第一门课的 session 引用。"""
    for event in occurrences_on(data, MONDAY):
        return f"{event['course_id']}:{event['session_id']}"
    raise AssertionError("示例课表第一周周一应有课")


def test_cancel_removes_only_that_date(data):
    ref = _monday_ref(data)
    patched = copy.deepcopy(data)
    patched["overrides"] = [{"session_ref": ref, "type": "cancel", "date": MONDAY.isoformat()}]

    assert ref not in {f"{e['course_id']}:{e['session_id']}" for e in occurrences_on(patched, MONDAY)}
    # 第二天不受影响
    assert occurrences_on(patched, TUESDAY) == occurrences_on(data, TUESDAY)


def test_cancel_without_date_applies_every_week(data):
    ref = _monday_ref(data)
    patched = copy.deepcopy(data)
    patched["overrides"] = [{"session_ref": ref, "cancelled": True}]

    for day in (MONDAY, MONDAY.replace(day=7)):
        refs = {f"{e['course_id']}:{e['session_id']}" for e in occurrences_on(patched, day)}
        assert ref not in refs


def test_same_day_move_shifts_time_and_location(data):
    ref = _monday_ref(data)
    patched = copy.deepcopy(data)
    patched["overrides"] = [
        {"session_ref": ref, "type": "move", "start": "18:00", "end": "19:30", "location": "中区 实验楼 305"}
    ]

    moved = [e for e in occurrences_on(patched, MONDAY) if f"{e['course_id']}:{e['session_id']}" == ref]
    assert len(moved) == 1
    assert (moved[0]["start"], moved[0]["end"]) == ("18:00", "19:30")
    assert moved[0]["location"] == "中区 实验楼 305"
    assert moved[0]["source"] == "override"
    # 改到 18:00 后应排在当天最后
    assert occurrences_on(patched, MONDAY)[-1]["start"] == "18:00"


def test_move_to_another_date(data):
    ref = _monday_ref(data)
    patched = copy.deepcopy(data)
    patched["overrides"] = [{"session_ref": ref, "type": "move", "date": TUESDAY.isoformat()}]

    monday_refs = {f"{e['course_id']}:{e['session_id']}" for e in occurrences_on(patched, MONDAY)}
    assert ref not in monday_refs

    tuesday = occurrences_on(patched, TUESDAY)
    assert ref in {f"{e['course_id']}:{e['session_id']}" for e in tuesday}
    assert [e for e in tuesday if f"{e['course_id']}:{e['session_id']}" == ref][0]["source"] == "override"


def test_unknown_override_shape_is_ignored(data):
    ref = _monday_ref(data)
    patched = copy.deepcopy(data)
    patched["overrides"] = [{"session_ref": ref, "whatever": 123}]
    assert occurrences_on(patched, MONDAY) == occurrences_on(data, MONDAY)


def test_broken_ref_and_bad_date_do_not_crash(data):
    patched = copy.deepcopy(data)
    patched["overrides"] = [
        {"session_ref": "nope:nope", "type": "cancel"},
        {"session_ref": _monday_ref(data), "type": "cancel", "date": "not-a-date"},
    ]
    # 坏 ref 与坏日期都应被静默忽略，课表保持原样
    assert occurrences_on(patched, MONDAY) == occurrences_on(data, MONDAY)


def test_exams_are_listed_for_their_date(data):
    patched = copy.deepcopy(data)
    patched["exams"] = [
        {
            "id": "e1",
            "course": "软件工程",
            "type": "期末考试",
            "time": MONDAY.isoformat(),
            "start_at": "09:00",
            "end_at": "11:00",
            "location": {"raw": "西区守正楼 101"},
        }
    ]
    assert [e["id"] for e in exams_on(patched, MONDAY)] == ["e1"]
    assert exams_on(patched, TUESDAY) == []

    text = format_exams(patched, MONDAY)
    assert "软件工程" in text and "期末考试" in text and "09:00-11:00" in text
    assert "没有考试" in format_exams(patched, TUESDAY)


def test_agenda_includes_exam_line(data):
    patched = copy.deepcopy(data)
    patched["exams"] = [
        {
            "id": "e1",
            "course": "软件工程",
            "type": "期末考试",
            "time": MONDAY.isoformat(),
            "start_at": "09:00",
            "end_at": "11:00",
            "location": {"raw": "西区守正楼 101"},
        }
    ]
    from timetable_service import format_agenda

    text = format_agenda(patched, MONDAY)
    assert "【期末考试】软件工程" in text


def test_morning_report_setting_roundtrip(store):
    store.import_example("alice")
    assert store.morning_report_time("alice") is None

    store.set_setting("morning_report:alice", "07:30")
    assert store.morning_report_time("alice") == time(7, 30)

    store.set_setting("morning_report:alice", None)
    assert store.morning_report_time("alice") is None


def test_users_lists_timetables_only(store):
    store.import_example("alice")
    store.bind_session("alice", "aiocqhttp:FriendMessage:1")
    store.set_setting("morning_report:alice", "07:30")
    store.mark_sent(["k"], )
    assert store.users() == ["alice"]


@pytest.fixture
def store(tmp_path):
    return TimetableStore(tmp_path, EXAMPLE)
