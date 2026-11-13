# AstrBot 课程表插件

一个以 `dev/schema.json` 为数据契约的个人课表插件。每位用户的数据单独保存在 AstrBot 插件数据目录（`data/plugin_data/astrbot_plugin_courses/<user-id>.json`），重装插件不会丢数据，也不会与其他用户混用。

## 已实现能力

- 按自然日期展开学期内的循环课程，返回课程、节次、真实时间、地点和教师。
- LLM 工具：查询、导入、添加/更新/删除排课，以及设置/删除提醒；删除和整表覆盖在调用前要求 LLM 取得用户确认。
- `/课表 今天`、`/课表 明天`、`/课表 日期 2026-09-01`、`/课表 提醒`。
- `/课表 导入示例` 将 `dev/course.json` 写入当前用户的私有课表，便于试用（会覆盖原有课表）。
- 课前提醒自动推送到该用户最近一次对话的会话。

导入会覆盖该用户的现有课表；LLM 在调用 `timetable_import` 前应取得明确确认。排课编辑通过 `课程 ID + session ID` 精确定位，例如 `2002002:s1`。

## 提醒投递

插件持久化“哪个排课、提前多少分钟”的提醒规则，并由 `initialize()` 启动的后台循环每 60 秒巡检一次：

1. 用 `pending_reminders()` 结合 `occurrences_on()` 算出已进入提醒窗口、且尚未推送的课程；
2. 通过 `context.send_message()` 推送到该用户**最近一次对话**的会话（`sessions.json` 记录，每次交互自动刷新）；
3. 以 `session_ref@开课日期` 作为幂等键写入 `sent_reminders.json`，只保留 7 天，重复 tick 或进程重启都不会重复推送。

已知边界：会话寻址依赖用户至少与插件交互过一次；用户换会话后，提醒会跟到新会话。若推送时平台返回失败，该条不记幂等键，下一轮 tick 会重试。

## 数据模型

`dev/schema.json` 是课表 JSON Schema，`dev/course.json` 是完整示例。`calendar.first_monday` 规定第一周周一，课程 session 使用 ISO 星期编号（1 为周一）及节次编号；插件据此计算实际日期和起止时间。

## 开发验证

```bash
python -m pytest
```

业务逻辑位于 `timetable_service.py`，不依赖 AstrBot，便于独立测试。
