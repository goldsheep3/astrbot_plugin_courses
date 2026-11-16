"""AstrBot adapters for the personal timetable service."""

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

try:  # AstrBot 以 data.plugins.<dir>.main 的形式按子包导入
    from . import llm_polish
    from . import timetable_edit as edit
    from .timetable_service import (
        TimetableError,
        TimetableStore,
        format_agenda,
        format_exams,
        format_reminder,
        pending_reminders,
    )
except ImportError:  # 直接以脚本/测试方式运行时
    import llm_polish
    import timetable_edit as edit
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


@register("course_timetable", "GoldSheep3", "可由 LLM 查询和编辑的个人课程表", "0.3.0")
class CourseTimetablePlugin(Star):
    REMINDER_TICK_SECONDS = 60

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        base_dir = Path(__file__).resolve().parent
        self.store = TimetableStore(_default_data_root(), base_dir / "dev" / "course.json")
        self._config = config or {}
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

    def _config_value(self, key: str, default):
        """读插件配置，兼容 AstrBotConfig 与普通 dict；类型不对时用默认值。"""
        try:
            value = self._config.get(key, default)
        except AttributeError:
            return default
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(default if value is None else value)
        if isinstance(default, float):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default
        if isinstance(default, int):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        return default if value is None else value

    async def _polish_text(
        self,
        base_text: str,
        facts: list[str],
        umo: str | None,
        *,
        enabled: bool,
        extra_hint: str | None = None,
    ) -> str:
        """让 LLM 改写文案；任何异常、超时或事实丢失都静默回落到模板。"""
        if not enabled or not base_text.strip():
            return base_text
        try:
            provider = await self.context.get_using_provider_async(umo=umo)
        except Exception as exc:  # 没配 provider 时这里就可能抛
            self.logger.debug(f"提醒润色跳过（无可用 provider）：{exc}")
            return base_text
        if provider is None:
            return base_text

        async def generate(system_prompt: str, user_prompt: str) -> str:
            response = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            return getattr(response, "completion_text", "") or ""

        return await llm_polish.polish_text(
            base_text,
            facts,
            generate,
            timeout=self._config_value("llm_timeout_seconds", 8.0),
            max_extra_chars=self._config_value("llm_max_extra_chars", 120),
            extra_hint=extra_hint,
        )

    @staticmethod
    def _agenda_facts(base_text: str) -> list[str]:
        """从早报模板里抠出必须原样保留的时间与地点。"""
        facts: list[str] = []
        for line in base_text.splitlines():
            if "｜" in line:
                facts.append(line.split("｜", 1)[1].strip())
            for token in re.findall(r"\d{1,2}:\d{2}", line):
                facts.append(token)
        return facts

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
                agenda = await self._polish_text(
                    agenda,
                    self._agenda_facts(agenda),
                    session,
                    enabled=self._config_value("llm_polish", True)
                    and self._config_value("llm_polish_morning_report", True),
                    extra_hint="早报，语气轻快，顺带提醒做好准备",
                )
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
                    base = format_reminder(item)
                    facts = [
                        item.get("course_name"),
                        item.get("start"),
                        item.get("end"),
                        item.get("location"),
                    ]
                    text = await self._polish_text(
                        base,
                        [str(fact) for fact in facts if fact],
                        session,
                        enabled=self._config_value("llm_polish", True),
                    )
                    await self.context.send_message(session, MessageChain().message(text))
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
        """查询指定日期（YYYY-MM-DD）的全部安排：考试、调课/补课、常规课程（按此优先级展示）。"""
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


    # ---------------------------------------------------------------- 课程层

    @filter.llm_tool(name="timetable_query_course")
    async def timetable_query_course(self, event: AstrMessageEvent, course_key: str) -> str:
        """查询某门课程的详情（教师、各段排课、已设提醒）；course_key 传课程 id 或名称。"""
        try:
            user_id = self._touch_session(event)
            data = self.store.load(user_id)
            course, _ = edit.find_course(data, course_key)
            text = edit.describe_course(data, course)
            prefix = f"{course.get('id')}:"
            hits = [
                f"  {r.get('session_ref')} 提前 {r.get('minutes_before')} 分钟"
                for r in self.store.list_reminders(user_id)
                if str(r.get("session_ref", "")).startswith(prefix)
            ]
            if hits:
                text += "\n提醒：\n" + "\n".join(hits)
            return text
        except (TimetableError, ValueError) as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="timetable_add_course")
    async def timetable_add_course(self, event: AstrMessageEvent, course_json: str) -> str:
        """新增一门课程。course_json 需含 name 与 teachers（可为 ["王崇"]），可选 id 与 sessions。"""
        try:
            user_id = self._touch_session(event)
            data = self.store.load(user_id)
            course = edit.add_course(data, json.loads(course_json))
            self.store.save(user_id, data)
            return f"已新增课程「{course['name']}」（id {course['id']}）。"
        except (TimetableError, json.JSONDecodeError) as exc:
            return f"新增失败：{exc}"

    @filter.llm_tool(name="timetable_update_course")
    async def timetable_update_course(self, event: AstrMessageEvent, course_key: str, patch_json: str) -> str:
        """修改课程元信息（name / teachers / id）；排课请用 session 工具，patch_json 为对象。"""
        try:
            user_id = self._touch_session(event)
            data = self.store.load(user_id)
            course = edit.update_course(data, course_key, json.loads(patch_json))
            self.store.save(user_id, data)
            return f"已更新课程「{course['name']}」（id {course['id']}）。"
        except (TimetableError, json.JSONDecodeError) as exc:
            return f"更新失败：{exc}"

    @filter.llm_tool(name="timetable_delete_course")
    async def timetable_delete_course(self, event: AstrMessageEvent, course_key: str) -> str:
        """删除整门课程及其全部排课与提醒；这是破坏性操作，调用前必须向用户确认。"""
        try:
            user_id = self._touch_session(event)
            data = self.store.load(user_id)
            course, refs = edit.delete_course(data, course_key)
            self.store.save(user_id, data)
            removed = 0
            for course_id, session_id in refs:
                if self.store.delete_reminder(user_id, course_id, session_id):
                    removed += 1
            extra = f"，同时清理 {removed} 条提醒" if removed else ""
            return f"已删除课程「{course.get('name')}」及 {len(refs)} 段排课{extra}。"
        except (TimetableError, ValueError) as exc:
            return f"删除失败：{exc}"

    # ---------------------------------------------------------------- 考试层

    @filter.llm_tool(name="timetable_query_exam")
    async def timetable_query_exam(self, event: AstrMessageEvent, exam_key: str) -> str:
        """查询某场考试；exam_key 传考试 id、日期（YYYY-MM-DD）或科目名。传日期时返回当天全部考试。"""
        try:
            data = self.store.load(self._touch_session(event))
            try:
                target = date.fromisoformat(exam_key.strip())
            except ValueError:
                target = None
            if target is not None:
                same_day = [
                    e for e in data.get("exams", [])
                    if isinstance(e, dict) and str(e.get("time") or e.get("date")) == target.isoformat()
                ]
                if same_day:
                    return "\n".join(edit.describe_exam(e) for e in same_day)
            exam, _ = edit.find_exam(data, exam_key)
            return edit.describe_exam(exam)
        except (TimetableError, ValueError) as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="timetable_add_exam")
    async def timetable_add_exam(self, event: AstrMessageEvent, exam_json: str) -> str:
        """新增一场考试。exam_json 需含 course 与 time（YYYY-MM-DD），可选 start_at/end_at/location/type/id。"""
        try:
            user_id = self._touch_session(event)
            data = self.store.load(user_id)
            exam = edit.add_exam(data, json.loads(exam_json))
            self.store.save(user_id, data)
            return "已新增考试：" + edit.describe_exam(exam)
        except (TimetableError, json.JSONDecodeError) as exc:
            return f"新增失败：{exc}"

    @filter.llm_tool(name="timetable_update_exam")
    async def timetable_update_exam(self, event: AstrMessageEvent, exam_key: str, patch_json: str) -> str:
        """修改某场考试；exam_key 传 id、日期或科目名，patch_json 为要改的字段对象。"""
        try:
            user_id = self._touch_session(event)
            data = self.store.load(user_id)
            exam = edit.update_exam(data, exam_key, json.loads(patch_json))
            self.store.save(user_id, data)
            return "已更新考试：" + edit.describe_exam(exam)
        except (TimetableError, json.JSONDecodeError) as exc:
            return f"更新失败：{exc}"

    @filter.llm_tool(name="timetable_delete_exam")
    async def timetable_delete_exam(self, event: AstrMessageEvent, exam_key: str) -> str:
        """删除某场考试；这是破坏性操作，调用前必须向用户确认。"""
        try:
            user_id = self._touch_session(event)
            data = self.store.load(user_id)
            exam = edit.delete_exam(data, exam_key)
            self.store.save(user_id, data)
            return "已删除考试：" + edit.describe_exam(exam)
        except (TimetableError, ValueError) as exc:
            return f"删除失败：{exc}"

    # ---------------------------------------------------------------- 覆写层

    @filter.llm_tool(name="timetable_list_overrides")
    async def timetable_list_overrides(self, event: AstrMessageEvent) -> str:
        """列出全部覆写（停课/调课/补课）。"""
        try:
            data = self.store.load(self._touch_session(event))
            items = edit.list_overrides(data)
            if not items:
                return "当前没有覆写。"
            return "覆写列表（序号可用于后续操作）：\n" + "\n".join(items)
        except (TimetableError, ValueError) as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="timetable_add_override")
    async def timetable_add_override(self, event: AstrMessageEvent, override_json: str) -> str:
        """新增覆写。override_json 需含 session_ref（课程id:排课id）与 type（cancel/move/add）；move 与 add 还需 date（YYYY-MM-DD），可选 periods、location。move 的 from_date 留空表示整学期搬走，填了则只把那一天的那节挪到 date。"""
        try:
            user_id = self._touch_session(event)
            data = self.store.load(user_id)
            override = edit.add_override(data, json.loads(override_json))
            self.store.save(user_id, data)
            return "已新增覆写：" + edit.describe_override(data, override)
        except (TimetableError, json.JSONDecodeError) as exc:
            return f"新增失败：{exc}"

    @filter.llm_tool(name="timetable_update_override")
    async def timetable_update_override(self, event: AstrMessageEvent, override_key: str, patch_json: str) -> str:
        """修改一条覆写；override_key 传覆写 id 或列表序号，patch_json 为要改的字段对象。"""
        try:
            user_id = self._touch_session(event)
            data = self.store.load(user_id)
            override = edit.update_override(data, override_key, json.loads(patch_json))
            self.store.save(user_id, data)
            return "已更新覆写：" + edit.describe_override(data, override)
        except (TimetableError, json.JSONDecodeError) as exc:
            return f"更新失败：{exc}"

    @filter.llm_tool(name="timetable_delete_override")
    async def timetable_delete_override(self, event: AstrMessageEvent, override_key: str) -> str:
        """删除一条覆写；这是破坏性操作，调用前必须向用户确认。"""
        try:
            user_id = self._touch_session(event)
            data = self.store.load(user_id)
            override = edit.delete_override(data, override_key)
            self.store.save(user_id, data)
            return "已删除覆写：" + edit.describe_override(data, override)
        except (TimetableError, ValueError) as exc:
            return f"删除失败：{exc}"
