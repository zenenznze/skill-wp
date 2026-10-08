# 三 Agent 提示词测试与状态回传缺口（2026-10-03）

## 范围与结论

用户要求在当前项目新增三个 Agent，分别发送测试提示词，并把每个 Agent 的结果汇总回原对话；随后追问主控获知状态究竟依靠 until，还是 WP 自身监听 Herdr 真实状态。

测试基线：`93b965f0015e4cb63cbf5d7eb7d122971a01e190`。三个任务均为只读、空 write scope、最多一次尝试。未修改项目代码，未 push/publish/deploy；本记录是后续单独授权的文档交付。

**提示词收发及结果汇总通过；WP 原生主控唤醒和完整 acceptance 闭环未被证明。** 不应将本次结果描述为 WP 全链路验收成功。

## 操作与实际结果

本次会话未发现可调用的 `wp_task_*` 工具，使用现有 headless `node bin/wpctl.mjs init/run` 入口。每个 run 启动 detached supervisor 后立即返回，没有在主控工具调用中等待 Worker 终态。

每个提示词要求读取 `package.json` 的 name、完成一项加法，并在 result summary 中返回独立标记；只允许写本任务 HANDOFF/result 状态。

| 任务 ID | Herdr tab / pane | 返回标记 | 算式 | 确定性 review |
| --- | --- | --- | --- | --- |
| `20261003-smoke-31097469-1` | `w23:t3` / `w23:p3` | `AGENT_1_OK` | `2+3=5` | PASS |
| `20261003-smoke-31097469-2` | `w23:t4` / `w23:p4` | `AGENT_2_OK` | `3+4=7` | PASS |
| `20261003-smoke-31097469-3` | `w23:t5` / `w23:p5` | `AGENT_3_OK` | `4+5=9` | PASS |

三者返回的项目名均为 `skill-wp-control-plane`，result status 均为 `success`，`changed_files` 均为空。主控独立读取 package.json、重算三个算式并执行 `git status --short`，结果正确且工作树无改动。

## 本次真实状态链路

```text
WP supervisor -> Herdr agent wait/read -> Worker 结果文件
             -> deterministic review -> Jev semantic gate -> 持久任务状态

until -> 检查三个 result.json 存在且 task_id 匹配 -> 唤醒原对话
主控  -> 回读 result/resources/task/review/semantic -> 独立验证 -> 汇总
```

WP 的执行层确实使用 Herdr `agent wait/read` 获取生命周期与终端证据；但**这次唤醒主控的是 until 文件条件，不是 WP 向原对话发送的原生完成事件，也不是 until 监听 Herdr 状态**。

until 条件只证明三个匹配 task ID 的 result 文件已经出现，不检查 review 完成、Jev 判断、Herdr idle/done 或任务 acceptance。它在第 12 次检查时成功，检查间隔为 5 秒。唤醒后主控再次读取完整证据，三项 review 和 semantic 当时均已持久化。

本地证据位于 `${WP_STATE_DIR:-~/.agents/state/wp}/repos/<repo-id>/tasks/<task-id>/` 下的 `result.json`、`resources.json`、`task.json`、`reviews/attempt-01.json` 和 `semantic.json`。这些是本机运行状态，不是公共仓库交付物；不复制原始会话、provider 日志或机器绝对路径到 tracked set。

## 用户指出的问题与本次暴露的不足

1. **原生主控回传缺口。** 本次结果回到原对话依赖外部 until；后台 supervisor 存在不等于它能主动通知原主控会话。测试未证明不借助 until 也能自动汇总。
2. **状态源表达不够明确。** 最初“返回后自动汇总”的说明没有明确告知 until 是唤醒来源，容易让人误以为 WP 自身已经提供监听与回传闭环。应分别标注 Herdr 生命周期、Worker 自报、确定性 review、semantic gate 和 controller acceptance。
3. **文件出现不等于完成。** 当前 until 条件可能早于 supervisor review/semantic 持久化；result 也可能是 failed/blocked。必须在唤醒后复核，不能以文件存在宣布任务成功。
4. **正式 acceptance 仍被阻塞。** 三项 semantic 均为 `manual_review`，reason 为 `missing_api_key`，probability/confidence 为 null；supervisor 环境缺少所需 `TYPESAFE_API_KEY`，task status 均为 `blocked`。这只证明该执行环境缺少条目，不证明全局凭据存储不存在条目。未绕过 Jev，也未调用 accept；三个 tab 保留。
5. **覆盖范围很窄。** 本次只验证三个独立只读 Agent 的启动、提示词投递和结果汇总；未验证 writer 冲突、retry、crash/resume、失联、重复通知、accept 或资源关闭。主控最终没有额外重新探测三个 Herdr Agent 的实时生命周期，不能声称已直接验证它们全部 idle/done。
6. **测试终端清理未闭环。** 用户指出：测试结束后，理论上应由 WP 关闭本次创建的测试 Agent 终端，而不是留给用户手动处理。本次三个测试 tab 均未关闭，属于交付体验和资源生命周期的不足。当前直接原因是 semantic gate blocked，现行安全协议要求保留资源；这解释了为何未关闭，但不等于清理需求已满足。应设计明确的测试结束/取消与安全回收路径，核实 Agent 停止、结果已收集、无未交付改动及资源归属后关闭本任务创建的 tab；不得为了清理绕过正式 acceptance 门禁。本条仅记录不足，不授权立即关闭当前 blocked 资源。
7. **入口与预检体验。** Plugin tools 在本次会话未暴露，故走 CLI；最初 task ID 缺少八位日期前缀，被 schema 拒绝，随后改为合规 ID。拒绝发生在创建前，没有因此启动额外 Worker。

以上是测试观察与缺口记录，不是已实施的修复。

## 后续改进验收建议（尚未实现）

- WP 提供与原主控会话绑定的 durable 通知/唤醒机制，不要求调用 Agent 手工另建 until。
- 按 task ID、attempt、事件序列进行去重和消费确认；重启或延迟通知不能重复处理旧结果。
- 主控唤醒事件携带真实资源 ID 和明确阶段：result available、review PASS/RETRY/BLOCKED、semantic、acceptance；不得把 Herdr idle/done 当成功。
- 单独验证 result 先于 review 出现、semantic 缺 key/API 故障、原会话 reload、重复与延迟事件、多 Agent 部分失败等情况。
- 以“无需外部 until，三个任务的结果及阻塞原因自动回到原对话且不重复”为回传闭环验收目标；正式 accept 和安全清理仍受独立门禁约束。
- 测试终端回收也纳入验收：正常完成后自动安全关闭本次创建的测试 tab；blocked 时明确报告保留原因与待办，并提供经授权的结束/取消回收路径。不得关闭 adopted、未知或其他任务资源；最后回读 Herdr 拓扑确认清理结果。

相关实现与协议：[durable-supervisor.md](durable-supervisor.md)、[protocol.md](protocol.md)、`src/core/supervisor.ts`、`src/herdr/client.ts`。
