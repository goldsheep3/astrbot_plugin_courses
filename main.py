"""AstrBot adapters for the personal timetable service."""

import json
from datetime import date, timedelta
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from timetable_service import TimetableError, TimetableStore, format_agenda


@register("course_timetable", "YourName", "可由 LLM 查询和编辑的个人课程表", "0.1.0")
class CourseTimetablePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        base_dir = Path(__file__).resolve().parent
        self.store = TimetableStore(base_dir / "data", base_dir / "dev" / "course.json")

    def _user_id(self, event: AstrMessageEvent) -> str:
        get_id = getattr(event, "get_sender_id", None)
        return str(get_id() if get_id else event.get_sender_name())

    @filter.command("课表")
    async def timetable(self, event: AstrMessageEvent):
        """查询课表：/课表 今天、明天、日期 YYYY-MM-DD、导入示例、提醒"""
        argument = event.message_str.removeprefix("/课表").strip()
        user_id = self._user_id(event)
        try:
            if argument == "导入示例":
                data = self.store.import_example(user_id)
                yield event.plain_result(f"已导入「{data['title']}」示例课表。")
                return
            if argument == "提醒":
                reminders = self.store.list_reminders(user_id)
                text = "\n".join(f"{r['session_ref']}：提前 {r['minutes_before']} 分钟" for r in reminders)
                yield event.plain_result(text or "尚未设置提醒。可让 LLM 设置课前提醒。")
                return
            today = date.today()
            target = today if argument in ("", "今天") else today + timedelta(days=1) if argument == "明天" else date.fromisoformat(argument.removeprefix("日期 ").strip())
            yield event.plain_result(format_agenda(self.store.load(user_id), target))
        except (TimetableError, ValueError) as exc:
            yield event.plain_result(f"课表操作失败：{exc}")

    @filter.llm_tool(name="timetable_query")
    async def timetable_query(self, event: AstrMessageEvent, target_date: str) -> str:
        """查询指定日期（YYYY-MM-DD）的课程、时间、地点和教师。"""
        try:
            return format_agenda(self.store.load(self._user_id(event)), date.fromisoformat(target_date))
        except (TimetableError, ValueError) as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="timetable_import")
    async def timetable_import(self, event: AstrMessageEvent, timetable_json: str) -> str:
        """导入完整课程表 JSON；调用前必须向用户确认覆盖其现有课表。"""
        try:
            data = self.store.import_json(self._user_id(event), timetable_json)
            return f"已导入课表「{data['title']}」。"
        except TimetableError as exc:
            return f"导入失败：{exc}"

    @filter.llm_tool(name="timetable_update_session")
    async def timetable_update_session(self, event: AstrMessageEvent, course_id: str, session_id: str, session_json: str) -> str:
        """编辑一个已存在排课。session_json 必须包含 weeks、day、periods、location、teachers。"""
        try:
            self.store.replace_session(self._user_id(event), course_id, session_id, json.loads(session_json))
            return "排课已更新。"
        except (TimetableError, json.JSONDecodeError) as exc:
            return f"更新失败：{exc}"

    @filter.llm_tool(name="timetable_add_session")
    async def timetable_add_session(self, event: AstrMessageEvent, course_id: str, session_json: str) -> str:
        """向已存在课程添加一个排课。session_json 必须含唯一 id、weeks、day、periods、location、teachers。"""
        try:
            self.store.add_session(self._user_id(event), course_id, json.loads(session_json))
            return "排课已添加。"
        except (TimetableError, json.JSONDecodeError) as exc:
            return f"添加失败：{exc}"

    @filter.llm_tool(name="timetable_delete_session")
    async def timetable_delete_session(self, event: AstrMessageEvent, course_id: str, session_id: str) -> str:
        """删除指定排课；这是破坏性操作，调用前必须向用户确认。"""
        try:
            self.store.delete_session(self._user_id(event), course_id, session_id)
            return "排课已删除。"
        except TimetableError as exc:
            return f"删除失败：{exc}"

    @filter.llm_tool(name="timetable_set_reminder")
    async def timetable_set_reminder(self, event: AstrMessageEvent, course_id: str, session_id: str, minutes_before: int) -> str:
        """为课程排课设置课前提醒，minutes_before 范围为 0 到 1440。"""
        try:
            self.store.set_reminder(self._user_id(event), course_id, session_id, minutes_before)
            return f"已设置提醒：{course_id}:{session_id} 开始前 {minutes_before} 分钟。"
        except TimetableError as exc:
            return f"设置失败：{exc}"

    @filter.llm_tool(name="timetable_delete_reminder")
    async def timetable_delete_reminder(self, event: AstrMessageEvent, course_id: str, session_id: str) -> str:
        """删除指定排课的提醒；调用前必须向用户确认。"""
        try:
            deleted = self.store.delete_reminder(self._user_id(event), course_id, session_id)
            return "提醒已删除。" if deleted else "该排课没有提醒。"
        except TimetableError as exc:
            return f"删除失败：{exc}"
