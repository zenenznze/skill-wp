# wp 协议

## 角色

当前调用技能的 Agent 是主控 Agent。它负责仓库理解、目标拆解、范围、安全、路由、验收和 Git 交付。Claude Code、Codex Goal、Grok、Kimi 和 Pi 是可被调用的 Agent client；它们只拥有 execution root 内的有界实现职责。

可选的 Workflow Governance v1 在此之上增加 orchestrator、sole-writer、reviewer、
scout、monitor 和 human approver 槽位。每个写作用域只有一个 sole-writer；reviewer、
scout、monitor 只读；human approver 必须是真实人类，不能由 Agent 冒充。治理记录和
v2 task package 分开保存和校验，详见 [`workflow-governance-v1.md`](workflow-governance-v1.md)。

Codex Goal 是唯一具有原生 Goal/thread 恢复语义的客户端。其他客户端是有界 prompt route，`--continue` 只能在 runner 能提供上一轮上下文时创建新的尝试。

## 状态位置

Plugin 与 `wpctl` 的权威状态固定在 `${WP_STATE_DIR:-~/.agents/state/wp}`，不使用目标仓库的 `.agent/`。迁移窗口内的旧 Python 入口仍读取 checkout 内 `wp-state/`，只用于兼容；新任务不得继续依赖该位置：

```text
wp-state/repos/<repo-id>/
├── tasks/<task-id>/
│   ├── HANDOFF.md
│   ├── EXECUTOR_PROMPT.txt
│   ├── CODEX_GOAL.txt
│   ├── claude-settings.json
│   ├── budget-checkpoints.json
│   ├── result.schema.json
│   ├── result.json
│   └── revision-01.md
└── runs/<task-id>/attempt-01/
    ├── invocation.json
    ├── client logs
    └── native Goal/thread artifacts when applicable
```

`repo-id` 由目标仓库路径计算出的本地 hash 标识，合同不保存绝对机器路径。Plugin 状态、事件、review 和资源 receipt 位于用户级状态目录。旧状态必须先预览摘要与冲突，再原子复制并校验 digest；迁移不自动删除源目录。

## Codex 预算连续性

当调用显式提供正的 `--token-budget` 时，runner 消费已安装 Codex App
Server schema 声明的 `thread/goal/updated`：使用
`params.goal.tokensUsed` 和 `params.goal.tokenBudget` 作为与原生
`budgetLimited` 一致的 Goal accounting 水位，并用同一通知的 `threadId`、
`turnId` 调用 schema 声明的 `turn/steer`（`threadId`、`expectedTurnId`、
`input`）。`thread/tokenUsage/updated` 的 raw model totals 只持久化作诊断，
不能参与软阈值计算。上述字段和 RPC 会写入 invocation 的
`protocol_evidence`，避免静默猜测协议。

预算的 75% 和 90% 是同一授权预算 generation 内各自一次的软信号；一次更新
跨过两个阈值时按 75% 后 90% 发送。阈值使用整数比较，因此 overshoot 也算
跨越。runner 先把 claim 原子写入 skill-local `budget-checkpoints.json`，再
发送 steering，再写入发送结果。App Server 重启和同 thread resume 会复用同
generation，已 claim 阈值不会重复。显式授权更大的总预算会建立新的递增
generation，保留旧 generation 的证据并按新总预算重新计算阈值。软信号不停止
turn、不改变 Goal 状态、不增加预算；100% 仍由 Codex 原生 `budgetLimited`
触发既有 blocked/resume 合同。

## HANDOFF

HANDOFF 是唯一完整的人类可读任务状态源，但不是聊天记录，也不是源码副本。只保留会改变方案、范围、安全、验证或验收的事实：

- 可观察目标和验收命令；
- 相关仓库现状、权威文件和已知未知项；
- 约束、非目标、权限和兼容性边界；
- 已做的材料决策及原因；
- 进度、阻塞、变更文件和验证结果。

`Suggested Implementation` 只是当前方向和取舍。执行者可以在目标和边界内选择更合适的仓库原生方案，并记录重要偏离。

### Atomic Work Contract

新建任务包使用 `task_protocol_version: 2`，并在 `# Atomic Work Contract`
下提供 fenced JSON。契约必须声明一个 `single_outcome`、有界
`deliverables`/`write_scope`、布尔 `read_only`、独立的 `acceptance` 和
`resume_boundary`。写任务的 `write_scope` 不得为空；只读任务必须显式使用
空数组。占位符、空字段、绝对路径、路径穿越和 malformed JSON 都会在启动前
被拒绝；`# Acceptance Criteria` 必须有 checklist，`# Validation Commands`
必须有至少一条命令。

无版本 HANDOFF 会被明确标记为 `legacy`，但默认在 `run_task.py` 预检时拒绝。
只有显式传入 `--allow-legacy-task-package` 才能沿 bounded 兼容路径恢复；图入口
使用同名旗标，并将其转发给每个 legacy 子任务。缺失版本不代表满足 v2 契约，
malformed version 也不会自动迁移。详细层级、拆分标准和 graph 写集一致性见
[`atomic-work.md`](atomic-work.md)。

## 终态

任务终态只允许：

- `success`：执行者写出完整终态，主控随后独立验证并接受；
- `blocked`：存在明确外部阻塞，记录位置、尝试、证据、解除动作和恢复点；
- `failed`：实现错误、传输不可恢复、协议错误、验收失败或尝试耗尽。

runner 在启动前写入 provisional `failed` result，并在 HANDOFF 写入 `runner_sentinel`。执行者必须在终态时删除 sentinel、更新 HANDOFF status，并替换 result。runner 退出码为 0 只表示终态文件已经持久化，不表示任务成功。

## 验证分类与结果写入

validation.role省略时按required处理；required失败或未执行始终拒绝success。辅助诊断可显式diagnostic，但必须保留reason和evidence，不能将合同验收命令降级；至少一项required通过。分类是执行证据，不替代主控重跑完整合同验收，诊断失败仍进入语义审查证据。

Worker在任务根result.json原子写终态（创建parent后临时文件rename）；supervisor归档到attempt。仅写attempt副本不代表收件完成。state.atomicWrite已负责创建缺失目录；恢复保留失败、身份及尝试次数，不手工把已耗尽failed重置为成功。

## 尝试边界

统一入口默认 `max-attempts=3`。每次尝试都受 timeout 限制；相同根因连续两次出现后停止重复尝试，主控必须修改假设或路由。Codex Goal 还受显式 token budget 限制，预算不会自动增加。

## 验收

验收分层且不能相互推导：executor result 是执行证据，reviewer 给只读独立意见，
主控重跑验证并决定 acceptance，human signoff 只能由人类决定，deployment 还要独立
授权和运行检查。Herdr `done`/`idle`、进程退出或 Agent 自报完成不跨越任何一层。

`verify_result.py` 只检查终态协议：

1. 定位同一个 skill-local task state；
2. 校验 result 的 task ID、status、summary、changed files、validation 和 blocker；
3. 校验 `handoff_path` 位于 `wp-state/` 且与目标任务一致；
4. 校验 HANDOFF status 与 result status 一致；
5. 校验没有遗留 `runner_sentinel`；
6. 在 `--require-success` 下要求 status 为 `success`。

TypeScript durable supervisor 还会直接读取 `git status --porcelain=v1 -z`，把真实改动与 `write_scope` 和 `result.changed_files` 双向比对；越界、漏报或不存在的 claim 都产生 RETRY。每次 attempt 的 prompt、Herdr resources、terminal output、result 和 review 都归档。控制器重启后的 resume 会优先探测原资源并重连。

主控仍必须读取完整 HANDOFF、检查 Git status 和完整 diff，并独立重跑每条验收命令。任何执行者自报的测试、最终消息、进程退出码或 Goal `complete` 都不足以单独接受任务。当前 attempt 没有持久化 PASS review 时 accept 必须失败，tab 也不得改名。

## 不可做的事

执行者不能 push、publish、deploy、读取凭据、修改用户全局配置、扩展权限或删除目标外数据。wp 不负责安装、发现链接、checkout 同步和公开发布；这些由 `joe` 或项目级规则负责。
