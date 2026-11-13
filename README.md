# AstrBot 课程表插件

一个以 `dev/schema.json` 为数据契约的个人课表插件。每位用户的数据单独保存在 AstrBot 插件数据目录（`data/plugin_data/astrbot_plugin_courses/<user-id>.json`），重装插件不会丢数据，也不会与其他用户混用。

## 已实现能力

- 按自然日期展开学期内的循环课程，返回课程、节次、真实时间、地点和教师。
- 应用 `overrides`（停课 / 改时间地点 / 改日期 / 补课）与当天 `exams`，查询结果自动带上。
- LLM 工具：查询、考试查询、导入、添加/更新/删除排课，以及设置/删除提醒；删除和整表覆盖在调用前要求 LLM 取得用户确认。
- `/课表 今天`、`/课表 明天`、`/课表 日期 2026-09-01`、`/课表 考试 [今天|明天|YYYY-MM-DD]`、`/课表 提醒`。
- `/课表 早报 07:30` 开启每天定时推送当天课表，`/课表 早报 关` 关闭，`/课表 早报` 查看当前设置。
- `/课表 导入示例` 将 `dev/course.json` 写入当前用户的私有课表，便于试用（会覆盖原有课表）。
- 课前提醒自动推送到该用户最近一次对话的会话。

导入会覆盖该用户的现有课表；LLM 在调用 `timetable_import` 前应取得明确确认。排课编辑通过 `课程 ID + session ID` 精确定位，例如 `2002002:s1`。

## 提醒投递

插件持久化“哪个排课、提前多少分钟”的提醒规则，并由 `initialize()` 启动的后台循环每 60 秒巡检一次：

1. 用 `pending_reminders()` 结合 `occurrences_on()` 算出已进入提醒窗口、且尚未推送的课程；
2. 通过 `context.send_message()` 推送到该用户**最近一次对话**的会话（`sessions.json` 记录，每次交互自动刷新）；
3. 以 `session_ref@开课日期` 作为幂等键写入 `sent_reminders.json`，只保留 7 天，重复 tick 或进程重启都不会重复推送。

早报走同一条 tick：到达设定时间后推送当天完整课表（含考试），幂等键为 `morning@日期`，每天只发一次，错过时间点的补发窗口到当天结束。

已知边界：会话寻址依赖用户至少与插件交互过一次；用户换会话后，提醒会跟到新会话。若推送时平台返回失败，该条不记幂等键，下一轮 tick 会重试。

## overrides（格式待定）

`overrides` 的结构尚未定稿，`dev/schema.json` 里目前是提案版本，解析器按容错方式处理：

| 字段 | 含义 |
| --- | --- |
| `session_ref` | 必填，指向 `courses.id:session.id`，例如 `2114009:s1` |
| `type` | `cancel` 停课、`move` 调课、`add` 补课；缺省时按出现的字段推断 |
| `date` | 生效日期。`cancel` 省略表示整学期停课；`move` 表示调往该日期 |
| `start` / `end` / `periods` | 改后的上课时间，`periods` 优先于 `start`/`end` |
| `location` | 改后的地点，字符串或 `{raw: ...}` 均可 |

容错规则：`session_ref` 缺失或指向不存在的排课、日期无法解析、类型无法识别，都会被静默忽略，不会让整个查询失败。因此格式定稿后只需调整 `_override_kind` / `_override_date` 两处即可。

## 数据模型

`dev/schema.json` 是课表 JSON Schema，`dev/course.json` 是完整示例。`calendar.first_monday` 规定第一周周一，课程 session 使用 ISO 星期编号（1 为周一）及节次编号；插件据此计算实际日期和起止时间。

## 开发验证

```bash
python -m pytest
```

业务逻辑位于 `timetable_service.py`，不依赖 AstrBot，便于独立测试。
