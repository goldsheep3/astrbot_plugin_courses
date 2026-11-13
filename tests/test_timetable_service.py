import json
from datetime import date
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from timetable_service import TimetableStore, format_agenda, occurrences_on


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
