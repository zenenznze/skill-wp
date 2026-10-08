# 客户端探测

客户端 roster 是一次运行前的实时快照，不是固定岗位表，也不包含历史设备状态。当前调用技能的 Agent 是主控；被选中的一个客户端是 writer；主控在任务结束后负责独立 review。需要 cross-vendor review 时，主控可再启动另一个健康客户端，但 reviewer 不得直接改 writer 的 worktree。

## 生成 roster

```bash
python3 scripts/discover_executors.py --probe --out wp-state/roster.json
```

脚本探测已安装的 Claude Code、Codex、Grok、Kimi、Pi，记录：

- `name`、`path`、`version`；
- `vendor`；
- `transport`，包括 headless、tools、native_goal、single_shot 等能力；
- `models` 和 live catalog 中发现的模型；
- `health`：`available`、`degraded` 或 `unknown`。

`degraded` 客户端不得被自动或显式选择。没有 roster 时，`run_task.py` 仍按本机可执行命令的默认顺序路由，但会给出提示。

## 路由

- `fast`：Claude Code、Pi、Grok、Kimi、Codex Goal；
- `balanced`：Claude Code、Pi、Grok、Kimi、Codex Goal；
- `hard`：Codex Goal、Claude Code、Pi、Grok、Kimi；
- `--information-retrieval`/`--research`：Grok 优先。

旧字段 `tier` 和 `tiers_available` 可以继续出现在 roster 中，但只作为兼容输出；新代码使用 `level`/`capability` 的 `fast`、`balanced`、`hard`。
