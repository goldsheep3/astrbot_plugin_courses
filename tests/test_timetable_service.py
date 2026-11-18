import json
from datetime import date
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from timetable_service import TimetableStore, format_agenda, occurrences_on, week_view


EXAMPLE = PROJECT_ROOT / "dev" / "course.json"


def test_expands_known_monday_course():
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    events = occurrences_on(data, date(2026, 8, 31))
    assert [event["course_name"] for event in events] == ["系统仿真与调试软件应用", "软件工程", "程序设计基础"]
    assert events[2]["start"] == "16:10"


def test_store_is_scoped_to_each_user(tmp_path):
    store = TimetableStore(tmp_path, EXAMPLE)
    store.import_example("alice")
    assert "程序设计基础" in format_agenda(store.load("alice"), date(2026, 8, 31))
    assert not (tmp_path / "bob.json").exists()


def test_week_view_includes_moved_course_and_exam():
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    original = occurrences_on(data, date(2026, 8, 31))[0]
    ref = f"{original['course_id']}:{original['session_id']}"
    data["overrides"].append({
        "session_ref": ref,
        "type": "move",
        "from_date": "2026-08-31",
        "date": "2026-09-01",
    })

    week = week_view(data, date(2026, 9, 2))
    assert week["week_start"] == "2026-08-31"
    assert week["week_end"] == "2026-09-06"
    assert week["week_number"] == 1
    assert len(week["days"]) == 7
    assert all(f"{event['course_id']}:{event['session_id']}" != ref for event in week["days"][0]["courses"])
    assert any(event["source"] == "override" and f"{event['course_id']}:{event['session_id']}" == ref for event in week["days"][1]["courses"])

    exam_week = week_view(data, date(2027, 1, 9))
    assert exam_week["days"][5]["date"] == "2027-01-09"
    exam = data["exams"][0]
    course_name = next(course["name"] for course in data["courses"] if course["id"] == exam["course"])
    assert exam_week["days"][5]["exams"][0]["name"] == course_name
