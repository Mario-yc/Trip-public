# Rollback Supersession

Tags: rollback, supersession, superseded, 历史, 编辑, 回滚, 重新生成, 对话

Use this SOP when a user edits a previous conversation message.

- MVP uses single-branch supersession, not a multi-branch tree.
- Editing an active historical user turn restores the itinerary snapshot linked to that turn or the nearest previous version.
- Later turns must be marked superseded and excluded from future Agent context.
- If regenerate=true, generate from the edited user message and restored snapshot only.
- Never delete historical turns or versions; keep them available for audit.
