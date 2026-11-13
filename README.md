# AstrBot 课程表插件

一个以 `dev/schema.json` 为数据契约的个人课表插件。每位用户的数据单独保存在 `data/<user-id>.json`，不会与其他用户混用。

## 已实现能力

- 按自然日期展开学期内的循环课程，返回课程、节次、真实时间、地点和教师。
- LLM 工具：查询、导入、添加/更新/删除排课，以及设置/删除提醒；删除和整表覆盖在调用前要求 LLM 取得用户确认。
- `/课表 今天`、`/课表 明天`、`/课表 日期 2026-09-01`、`/课表 提醒`。
- `/课表 导入示例` 将 `dev/course.json` 写入当前用户的私有课表，便于试用。

导入会覆盖该用户的现有课表；LLM 在调用 `timetable_import` 前应取得明确确认。排课编辑通过 `课程 ID + session ID` 精确定位，例如 `2002002:s1`。

## 提醒边界

插件已持久化“哪个排课、提前多少分钟”的提醒规则。实际将提醒推送回原始会话，需要由 AstrBot 的平台适配器提供定时投递和会话寻址能力；模板项目没有这部分 API，因此当前版本不声称已经完成自动发信。接入该 API 后，应以 `TimetableStore.list_reminders()` 与 `occurrences_on()` 计算待发送提醒，并记录投递幂等键（`用户 + session_ref + 开课日期`）。

## 数据模型

`dev/schema.json` 是课表 JSON Schema，`dev/course.json` 是完整示例。`calendar.first_monday` 规定第一周周一，课程 session 使用 ISO 星期编号（1 为周一）及节次编号；插件据此计算实际日期和起止时间。

## 开发验证

```powershell
py -m pytest
```

业务逻辑位于 `timetable_service.py`，不依赖 AstrBot，便于独立测试。
