import json
from datetime import date, datetime, timedelta
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from timetable_service import (
    TimetableError,
    TimetableStore,
    format_reminder,
    pending_reminders,
    validate_timetable,
)

EXAMPLE = PROJECT_ROOT / "dev" / "course.json"


@pytest.fixture
def data():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path):
    return TimetableStore(tmp_path, EXAMPLE)


# 2026-08-31 是第一周周一，示例课表当天有三门课，最早 16:10 开始
MONDAY = date(2026, 8, 31)


def _first_session_ref(data):
    for course in data["courses"]:
        for session in course["sessions"]:
            if session["day"] == 1 and 1 in session["weeks"]:
                return f"{course['id']}:{session['id']}"
    raise AssertionError("示例课表缺少第一周周一课程")


def test_reminder_becomes_due_before_class(data):
    ref = _first_session_ref(data)
    reminders = [{"session_ref": ref, "minutes_before": 30, "enabled": True}]
    events = [e for e in pending_reminders(data, reminders, datetime(2026, 8, 31, 15, 45)) if e["key"] == f"{ref}@2026-08-31"]
    assert events, "课前 25 分钟应处于提醒窗口内"
    assert events[0]["course_name"]


def test_reminder_not_due_too_early_or_after_start(data):
    ref = _first_session_ref(data)
    reminders = [{"session_ref": ref, "minutes_before": 30, "enabled": True}]
    assert pending_reminders(data, reminders, datetime(2026, 8, 31, 9, 0)) == []
    assert pending_reminders(data, reminders, datetime(2026, 8, 31, 23, 0)) == []


def test_reminder_is_idempotent_via_sent_keys(data):
    ref = _first_session_ref(data)
    reminders = [{"session_ref": ref, "minutes_before": 30, "enabled": True}]
    now = datetime(2026, 8, 31, 15, 45)
    due = pending_reminders(data, reminders, now)
    assert due
    assert pending_reminders(data, reminders, now, {e["key"] for e in due}) == []


def test_disabled_reminder_is_skipped(data):
    ref = _first_session_ref(data)
    reminders = [{"session_ref": ref, "minutes_before": 30, "enabled": False}]
    assert pending_reminders(data, reminders, datetime(2026, 8, 31, 15, 45)) == []


def test_format_reminder_reads_naturally(data):
    ref = _first_session_ref(data)
    due = pending_reminders(data, [{"session_ref": ref, "minutes_before": 30}], datetime(2026, 8, 31, 15, 45))
    text = format_reminder(due[0])
    assert "30 分钟后开始" in text
    assert due[0]["location"] in text


def test_session_binding_and_sent_keys(store, data):
    store.import_example("alice")
    store.bind_session("alice", "aiocqhttp:FriendMessage:123")
    assert store.session_of("alice") == "aiocqhttp:FriendMessage:123"

    stamp = datetime(2026, 8, 31, 15, 45)
    store.mark_sent(["a@2026-08-31"], stamp)
    assert "a@2026-08-31" in store.sent_keys(stamp)
    # 超过 7 天的幂等键应被清理，否则文件会无限增长
    assert store.sent_keys(stamp + timedelta(days=8)) == set()


def test_users_with_reminders_lists_only_those_with_rules(store, data):
    ref = _first_session_ref(data)
    store.import_example("alice")
    store.import_example("bob")
    store.set_reminder("alice", *ref.split(":"), 15)
    assert store.users_with_reminders() == ["alice"]


def test_normalize_keeps_ids_path_safe(store):
    assert TimetableStore.normalize("aiocqhttp:123") == "aiocqhttp_123"
    assert TimetableStore.normalize("") == "anonymous"


def test_invalid_timetable_is_rejected(data):
    data["calendar"]["week_count"] = 0
    with pytest.raises(TimetableError):
        validate_timetable(data)


def test_reminder_range_is_validated(store):
    store.import_example("alice")
    with pytest.raises(TimetableError):
        store.set_reminder("alice", "2002002", "s1", 5000)
