---
name: wp
description: >-
  Use when an Agent needs to delegate a non-trivial repository implementation,
  refactor, debugging session, review, or bounded research pass to another Agent
  with durable HANDOFF state, visible Herdr execution, retry/resume, and independent
  acceptance; also use for WP task recovery, adoption of an existing Herdr tab,
  Codex Goal resume, Grok source retrieval, or wpctl compatibility. 当用户说“委派这个仓库的
  代码修改并保留可恢复交接”“让另一个 Agent 完成跨文件重构并检查最终 diff”、用 Grok
  检索资料后写入仓库，或要求在 Herdr 中启动可观察 Agent、恢复任务、接管标签页时使用。
---

<!-- ASM_SKILL_UPDATE_GATE:START -->
## Mandatory startup update gate

Before this skill performs operational work, resolve the real path of this
`SKILL.md` and its owning Git checkout, then run:

```bash
python3 ~/.agents/skills/asm/bin/skill_update_gate.py run --skill <absolute-path-to-this-SKILL.md>
```

Continue only when the gate reports `status: ready`. If it fast-forwards the
checkout, re-read this skill and every changed module or executable that will
be used. Stop for dirty, ahead, diverged, detached, wrong-origin,
missing-upstream, fetch-failed, or non-fast-forward state; never stash, reset,
merge, force-pull, overwrite, or execute stale skill-owned code. A network-only
failure permits only an explicitly read-only request marked freshness-unverified.
Run the gate once per checkout per activation; sibling skills in the same
checkout reuse the result.
<!-- ASM_SKILL_UPDATE_GATE:END -->

# WP：统一 Agent 调度

WP 同时提供两个入口，共享同一状态协议：

- **Skill 指南层（本文件）**：定义何时委派、任务边界、安全和主控验收责任。
- **Pi Plugin（`extensions/wp.ts`）**：确定性创建合同、启动 Herdr、校验结果、复核、恢复和验收。

正常 Pi 会话必须优先使用 Plugin 工具或 `/wp`；不要手工拼接 `herdr tab create → agent start → agent prompt`。`wpctl` 是 headless/恢复入口；`scripts/*.py` 只在迁移窗口内兼容旧调用方。

## 三层真源

1. **SOP/HANDOFF 管事实**：`HANDOFF.md`、`task.json`、`result.json`、`events.jsonl`、`attempts/`、`reviews/`、`checkpoints/`、`resources.json` 是可恢复状态；聊天和最终回答只是证据。
2. **WP Plugin 管逻辑**：创建合同、单 writer 锁、调度、结果 schema、独立 review、bounded retry、恢复和 acceptance gate。
3. **Herdr 管前台**：一个 Agent 一个可见 tab/pane；`idle`/`done` 只是生命周期，不代表任务成功。

状态默认位于 `~/.agents/state/wp/`，可用 `WP_STATE_DIR` 覆盖。目标仓库不写 `.agent/` 或过程状态。旧 checkout 内 `wp-state/` 只能通过 `wpctl migrate` 预览、校验后复制；不自动删除。

## 正常工作流

1. 读取目标仓库规则、Git 状态、权威实现和验收命令。
2. 把目标拆成单一可观察结果；声明 `deliverables`、`write_scope`、`read_only`、`acceptance`、`resume_boundary` 和最多尝试数。
3. 调用 `wp_task_create`；合同未通过严格 schema 前不得启动 Worker。
4. 调用 `wp_task_run`。该调用只启动独立的后台 supervisor 并立即返回；绝不能等待 Worker 终态或阻塞主控对话。Worker prompt 只携带 task ID、HANDOFF 路径、attempt 和边界；Worker 首先读取 HANDOFF。
5. 主控保持可交互，按需使用 `wp_task_status` 短调用查看持久状态与真实 Herdr resource IDs。不要在工具调用内持续轮询，也不要从 tab `idle/done` 推导成功。
6. 后台 durable supervisor 等待 Worker、归档 attempt 证据，并用真实 Git 变更和 result claims 做确定性 review；只允许 `PASS`、`RETRY`、`BLOCKED`，RETRY 自动进入有界下一尝试。`wp_task_resume` 同样只启动或确认后台 supervisor 后立即返回。
7. 确定性 PASS 后，Jev Choice 根据完整验收证据输出 `complete|retry|blocked`、概率与置信度。阈值由 `WP_JEV_MIN_CONFIDENCE`/`WP_JEV_MIN_PROBABILITY` 配置；缺少 `TYPESAFE_API_KEY`、低置信度、API/解析故障一律 fail-safe 保留资源并等待恢复或人工检查。
8. 控制器重启后 resume 优先探测并重连 `resources.json` 中的真实 tab/agent；不可达且无可收集结果时才创建后续资源。
9. 主控独立检查完整 diff、重跑验收命令；只有当前 attempt 的确定性 PASS、Jev confident `complete` 和主控接受后调用 `wp_task_accept`。Accept 只关闭明确标记 `created_by_wp` 且属于本任务的 tab；adopted、未知、失败或清理异常资源必须保留。

已有 tab 只可先 `wp_task_adopt(... confirmed:false)` 预览，再由用户确认身份、write scope 和状态后 `confirmed:true` 绑定。不得按 Herdr 生命周期自动接管。

## Pi 接口

- `/wp`：任务列表、Herdr 映射、恢复/接管入口和限时救援模式。
- Tools：`wp_task_create`、`wp_task_status`、`wp_task_run`、`wp_task_resume`、`wp_task_review`、`wp_task_adopt`、`wp_task_accept`。
- Plugin 会阻止普通 bash 工具直接执行常见 Herdr 调度链。纯查看/focus/read/snapshot、Herdr 自身诊断，或 `/wp` 显式开启的限时救援不受阻；救援不能静默跨越任务状态。

## Headless CLI

```bash
wpctl init --repo <root> --task-id <yyyymmdd-slug> --goal <goal> \
  --write-scope src/a.ts,tests/a.test.ts --acceptance "test passes|diff is scoped"
wpctl run --repo <root> --task-id <id> --controller-tab-id <workspace:tab>
wpctl status --repo <root> --task-id <id>
wpctl check --repo <root> --task-id <id>
wpctl accept --repo <root> --task-id <id>
wpctl migrate --legacy-root <skill-checkout>/wp-state       # preview
wpctl migrate --legacy-root <skill-checkout>/wp-state --apply
```

## 安全与失败边界

- Worker 不得 push、publish、deploy、读取凭据、修改用户全局配置或扩大 write scope。
- 凭据不得进入 HANDOFF、prompt、events、result、review 或 checkpoint；运行时环境只传给获授权进程。
- Herdr 不可用、tab 被关闭、Pi/Plugin 重启或资源恢复失败时保留 SOP/checkpoint，进入 blocked 或 bounded retry，不丢状态。
- 合同 digest 不一致、多控制器写锁冲突、路径穿越或非法 schema 一律 fail closed。
- 未知或并发 Git 修改不得 reset、stash、覆盖或夹带。
- review、主控验收、human signoff、push 和 deployment 是独立门禁，不能互相推导。

## 详细参考

- `references/protocol.md`：状态、合同、终态和验收层级。
- `references/durable-supervisor.md`：Herdr 调度、review/retry 和 crash-safe resume。
- `references/graph-orchestration.md`：依赖图、ready-set 和 write-scope 冲突。
- `README.md`：安装、Plugin 暴露、`wpctl` 与迁移说明。

## Skill Handoffs

- 技能内容创建、评测和自更新：load `asm` skill 的 `asm-make` 路由。
- Skill/Plugin checkout 与 discovery link 安装、暴露和修复：load `asm` skill。
- 本仓库验证、commit、push 和远端核验：load `dev` skill。
- Herdr 通用布局、诊断和原始控制：load `herdr` skill；正常任务调度仍由 WP Plugin 纳管。
