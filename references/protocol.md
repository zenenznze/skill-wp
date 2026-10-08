# wp 协议

## 角色

当前调用技能的 Agent 是主控 Agent。它负责仓库理解、目标拆解、范围、安全、路由、验收和 Git 交付。Claude Code、Codex Goal、Grok、Kimi 和 Pi 是可被调用的 Agent client；它们只拥有 execution root 内的有界实现职责。

Codex Goal 是唯一具有原生 Goal/thread 恢复语义的客户端。其他客户端是有界 prompt route，`--continue` 只能在 runner 能提供上一轮上下文时创建新的尝试。

## 状态位置

wp 的持久状态固定在技能 checkout 的 `wp-state/`，不使用目标仓库的 `.agent/`：

```text
wp-state/repos/<repo-id>/
├── tasks/<task-id>/
│   ├── HANDOFF.md
│   ├── EXECUTOR_PROMPT.txt
│   ├── CODEX_GOAL.txt
│   ├── claude-settings.json
│   ├── result.schema.json
│   ├── result.json
│   └── revision-01.md
└── runs/<task-id>/attempt-01/
    ├── invocation.json
    ├── client logs
    └── native Goal/thread artifacts when applicable
```

`repo-id` 由目标仓库路径计算出的本地 hash 标识，不写绝对机器路径。任务状态和原始 transport 日志均为技能目录内的本地运行记录。

## HANDOFF

HANDOFF 是唯一完整的人类可读任务状态源，但不是聊天记录，也不是源码副本。只保留会改变方案、范围、安全、验证或验收的事实：

- 可观察目标和验收命令；
- 相关仓库现状、权威文件和已知未知项；
- 约束、非目标、权限和兼容性边界；
- 已做的材料决策及原因；
- 进度、阻塞、变更文件和验证结果。

`Suggested Implementation` 只是当前方向和取舍。执行者可以在目标和边界内选择更合适的仓库原生方案，并记录重要偏离。

## 终态

任务终态只允许：

- `success`：执行者写出完整终态，主控随后独立验证并接受；
- `blocked`：存在明确外部阻塞，记录位置、尝试、证据、解除动作和恢复点；
- `failed`：实现错误、传输不可恢复、协议错误、验收失败或尝试耗尽。

runner 在启动前写入 provisional `failed` result，并在 HANDOFF 写入 `runner_sentinel`。执行者必须在终态时删除 sentinel、更新 HANDOFF status，并替换 result。runner 退出码为 0 只表示终态文件已经持久化，不表示任务成功。

## 尝试边界

统一入口默认 `max-attempts=3`。每次尝试都受 timeout 限制；相同根因连续两次出现后停止重复尝试，主控必须修改假设或路由。Codex Goal 还受显式 token budget 限制，预算不会自动增加。

## 验收

`verify_result.py` 只检查终态协议：

1. 定位同一个 skill-local task state；
2. 校验 result 的 task ID、status、summary、changed files、validation 和 blocker；
3. 校验 `handoff_path` 位于 `wp-state/` 且与目标任务一致；
4. 校验 HANDOFF status 与 result status 一致；
5. 校验没有遗留 `runner_sentinel`；
6. 在 `--require-success` 下要求 status 为 `success`。

主控仍必须读取完整 HANDOFF、检查 Git status 和完整 diff，并独立重跑每条验收命令。任何执行者自报的测试、最终消息、进程退出码或 Goal `complete` 都不足以单独接受任务。

## 不可做的事

执行者不能 push、publish、deploy、读取凭据、修改用户全局配置、扩展权限或删除目标外数据。wp 不负责安装、发现链接、checkout 同步和公开发布；这些由 `agent-skill-sync` 或项目级规则负责。
