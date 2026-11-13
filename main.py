"""AstrBot adapters for the personal timetable service."""

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

try:  # AstrBot 以 data.plugins.<dir>.main 的形式按子包导入
    from .timetable_service import (
        TimetableError,
        TimetableStore,
        format_agenda,
        format_exams,
        format_reminder,
        pending_reminders,
    )
except ImportError:  # 直接以脚本/测试方式运行时
    from timetable_service import (
        TimetableError,
        TimetableStore,
        format_agenda,
        format_exams,
        format_reminder,
        pending_reminders,
    )


def _default_data_root() -> Path:
    """Use AstrBot's plugin data directory so reinstalling the plugin keeps data."""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_courses"
    except Exception:  # pragma: no cover - standalone/test usage
        return Path(__file__).resolve().parent / "data"


@register("course_timetable", "GoldSheep3", "可由 LLM 查询和编辑的个人课程表", "0.2.0")
class CourseTimetablePlugin(Star):
    REMINDER_TICK_SECONDS = 60

    def __init__(self, context: Context):
        super().__init__(context)
        base_dir = Path(__file__).resolve().parent
        self.store = TimetableStore(_default_data_root(), base_dir / "dev" / "course.json")
        self._reminder_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        self._reminder_task = asyncio.create_task(self._reminder_loop())

    async def terminate(self) -> None:
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            try:
                await self._reminder_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reminder_task = None

    def _user_id(self, event: AstrMessageEvent) -> str:
        get_id = getattr(event, "get_sender_id", None)
        raw = str(get_id() if get_id else event.get_sender_name())
        platform = getattr(event, "get_platform_name", None)
        prefix = platform() if callable(platform) else ""
        return TimetableStore.normalize(f"{prefix}:{raw}" if prefix else raw)

    def _touch_session(self, event: AstrMessageEvent) -> str:
        """Bind the current chat as the reminder delivery target."""
        user_id = self._user_id(event)
        session = getattr(event, "unified_msg_origin", None) or getattr(event, "session", None)
        if session:
            try:
                self.store.bind_session(user_id, str(session))
            except OSError:
                pass
        return user_id

    async def _reminder_loop(self) -> None:
        while True:
            try:
                await self._dispatch_morning_reports()
                await self._dispatch_reminders()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the loop alive
                self.logger.warning(f"课程提醒巡检失败：{exc}")
            await asyncio.sleep(self.REMINDER_TICK_SECONDS)

    async def _dispatch_morning_reports(self) -> None:
        """Push today's agenda once per day at the configured time."""
        now = datetime.now()
        sent = self.store.sent_keys(now)
        for user_id in self.store.users():
            raw = self.store.get_setting(f"morning_report:{user_id}")
            if not raw:
                continue
            try:
                report_time = datetime.strptime(str(raw), "%H:%M").time()
            except ValueError:
                continue
            if now.time() < report_time:
                continue
            key = f"morning@{now.date().isoformat()}"
            if key in sent:
                continue
            session = self.store.session_of(user_id)
            if not session:
                continue
            try:
                agenda = format_agenda(self.store.load(user_id), now.date())
                await self.context.send_message(session, MessageChain().message(agenda))
            except Exception as exc:
                self.logger.warning(f"早报推送失败（{user_id}）：{exc}")
                continue
            self.store.mark_sent([key], now)

    async def _dispatch_reminders(self) -> None:
        now = datetime.now()
        sent = self.store.sent_keys(now)
        for user_id in self.store.users_with_reminders():
            session = self.store.session_of(user_id)
            if not session:
                continue
            try:
                data = self.store.load(user_id)
                due = pending_reminders(
                    data,
                    self.store.list_reminders(user_id),
                    now,
                    {key.split("|", 1)[1] for key in sent if key.startswith(f"{user_id}|")},
                )
            except TimetableError as exc:
                self.logger.warning(f"用户 {user_id} 的课表或提醒无效，跳过：{exc}")
                continue
            delivered: list[str] = []
            for item in due:
                try:
                    await self.context.send_message(session, MessageChain().message(format_reminder(item)))
                except Exception as exc:
                    self.logger.warning(f"提醒推送失败（{item['key']}）：{exc}")
                    continue
                delivered.append(f"{user_id}|{item['key']}")
            if delivered:
                self.store.mark_sent(delivered, now)

    @filter.command("课表")
    async def timetable(self, event: AstrMessageEvent):
        """查询课表：/课表 今天、明天、日期 YYYY-MM-DD、考试、提醒、早报 HH:MM、导入示例"""
        argument = event.message_str.removeprefix("/课表").strip()
        user_id = self._touch_session(event)
        try:
            if argument == "导入示例":
                data = self.store.import_example(user_id)
                yield event.plain_result(f"已导入「{data['title']}」示例课表（覆盖原有课表）。")
                return
            if argument.startswith("早报"):
                setting = argument.removeprefix("早报").strip()
                if setting in ("关", "关闭", "off"):
                    self.store.set_setting(f"morning_report:{user_id}", None)
                    yield event.plain_result("早报已关闭。")
                    return
                if not setting:
                    current = self.store.get_setting(f"morning_report:{user_id}")
                    yield event.plain_result(
                        f"当前早报时间：{current}" if current else "早报未开启。用法：/课表 早报 07:30"
                    )
                    return
                try:
                    datetime.strptime(setting, "%H:%M")
                except ValueError:
                    yield event.plain_result("时间格式应为 HH:MM，例如 /课表 早报 07:30")
                    return
                self.store.set_setting(f"morning_report:{user_id}", setting)
                yield event.plain_result(f"早报已设置：每天 {setting} 推送当天课表。")
                return
            if argument.startswith("考试"):
                target_text = argument.removeprefix("考试").strip()
                today = date.today()
                if target_text in ("", "今天"):
                    target = today
                elif target_text == "明天":
                    target = today + timedelta(days=1)
                else:
                    target = date.fromisoformat(target_text.removeprefix("日期 ").strip())
                yield event.plain_result(format_exams(self.store.load(user_id), target))
                return
            if argument == "提醒":
                reminders = self.store.list_reminders(user_id)
                if not reminders:
                    yield event.plain_result("尚未设置提醒。可让 LLM 设置课前提醒。")
                    return
                lines = [
                    f"{r['session_ref']}：提前 {r['minutes_before']} 分钟"
                    for r in reminders
                    if r.get("enabled", True)
                ]
                lines.append("到点会自动推送到最近一次对话的会话。")
                yield event.plain_result("\n".join(lines))
                return
            today = date.today()
            if argument in ("", "今天"):
                target = today
            elif argument == "明天":
                target = today + timedelta(days=1)
            else:
                target = date.fromisoformat(argument.removeprefix("日期 ").strip())
            yield event.plain_result(format_agenda(self.store.load(user_id), target))
        except (TimetableError, ValueError) as exc:
            yield event.plain_result(f"课表操作失败：{exc}")

    @filter.llm_tool(name="timetable_query")
    async def timetable_query(self, event: AstrMessageEvent, target_date: str) -> str:
        """查询指定日期（YYYY-MM-DD）的课程、时间、地点和教师。"""
        try:
            return format_agenda(self.store.load(self._touch_session(event)), date.fromisoformat(target_date))
        except (TimetableError, ValueError) as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="timetable_import")
    async def timetable_import(self, event: AstrMessageEvent, timetable_json: str) -> str:
        """导入完整课程表 JSON；调用前必须向用户确认覆盖其现有课表。"""
        try:
            data = self.store.import_json(self._touch_session(event), timetable_json)
            return f"已导入课表「{data['title']}」。"
        except TimetableError as exc:
            return f"导入失败：{exc}"

    @filter.llm_tool(name="timetable_update_session")
    async def timetable_update_session(self, event: AstrMessageEvent, course_id: str, session_id: str, session_json: str) -> str:
        """编辑一个已存在排课。session_json 必须包含 weeks、day、periods、location、teachers。"""
        try:
            self.store.replace_session(self._touch_session(event), course_id, session_id, json.loads(session_json))
            return "排课已更新。"
        except (TimetableError, json.JSONDecodeError) as exc:
            return f"更新失败：{exc}"

    @filter.llm_tool(name="timetable_add_session")
    async def timetable_add_session(self, event: AstrMessageEvent, course_id: str, session_json: str) -> str:
        """向已存在课程添加一个排课。session_json 必须含唯一 id、weeks、day、periods、location、teachers。"""
        try:
            self.store.add_session(self._touch_session(event), course_id, json.loads(session_json))
            return "排课已添加。"
        except (TimetableError, json.JSONDecodeError) as exc:
            return f"添加失败：{exc}"

    @filter.llm_tool(name="timetable_delete_session")
    async def timetable_delete_session(self, event: AstrMessageEvent, course_id: str, session_id: str) -> str:
        """删除指定排课；这是破坏性操作，调用前必须向用户确认。"""
        try:
            self.store.delete_session(self._touch_session(event), course_id, session_id)
            return "排课已删除。"
        except TimetableError as exc:
            return f"删除失败：{exc}"

    @filter.llm_tool(name="timetable_exams")
    async def timetable_exams(self, event: AstrMessageEvent, target_date: str) -> str:
        """查询指定日期（YYYY-MM-DD）的考试安排，含科目、类型、时间与地点。"""
        try:
            return format_exams(self.store.load(self._touch_session(event)), date.fromisoformat(target_date))
        except (TimetableError, ValueError) as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="timetable_set_reminder")
    async def timetable_set_reminder(self, event: AstrMessageEvent, course_id: str, session_id: str, minutes_before: int) -> str:
        """为课程排课设置课前提醒，minutes_before 范围为 0 到 1440。"""
        try:
            self.store.set_reminder(self._touch_session(event), course_id, session_id, minutes_before)
            return f"已设置提醒：{course_id}:{session_id} 开始前 {minutes_before} 分钟。"
        except TimetableError as exc:
            return f"设置失败：{exc}"

    @filter.llm_tool(name="timetable_delete_reminder")
    async def timetable_delete_reminder(self, event: AstrMessageEvent, course_id: str, session_id: str) -> str:
        """删除指定排课的提醒；调用前必须向用户确认。"""
        try:
            deleted = self.store.delete_reminder(self._touch_session(event), course_id, session_id)
            return "提醒已删除。" if deleted else "该排课没有提醒。"
        except TimetableError as exc:
            return f"删除失败：{exc}"
