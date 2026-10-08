# 停滞、失败和恢复

难、慢或普通不确定性都不是 blocker。主控先读新证据、修改假设或更换路由，在当前 attempt budget 内继续。

## 退出判断

| 情况 | 处理 | 终态 |
|---|---|---|
| 实现失败但还有新假设 | 修正方案并继续 | 非终态 |
| 验收失败 | 修复并重新运行 | 非终态 |
| 客户端启动失败、崩溃或超时 | 记录 transport 证据；可切换客户端或进入下一次尝试 | 最终为 `failed`，除非是 Codex 的明确可恢复限制 |
| 相同根因连续出现 2 次 | 停止重复重试，改路由或结束任务 | `failed` |
| 缺少凭据、外部服务或用户无法从仓库推断的关键决定 | 记录准确位置、尝试、证据、解除动作和恢复点 | `blocked` |
| Codex `usageLimited` | 保留同一 thread，等待额度恢复后由主控显式继续 | `blocked` |
| Codex `budgetLimited` | 主控审查进度并显式授权新的总预算 | `blocked` |
| Codex 75%/90% soft checkpoint | runner 持久化并通过活动回合 steering 提醒，Goal 继续运行 | 非终态 |
| Codex sustained timebox 到期 | 主控审查进度并显式授权下一个窗口 | `blocked` |
| Codex App Server 第一次退出 | 同一 thread 重启并恢复一次 | 非终态 |
| Codex App Server 第二次退出 | 不再重启 | `failed` |
| Goal 无事件 | 查询 Goal；仍 active 时发送一次继续信号 | 第二次无事件为 `failed` |
| Goal complete 但终态文件无效 | 发送一次仅修复 HANDOFF/result 的 turn | 第二次无效为 `failed` |

普通客户端的 `--continue` 会创建新的编号 attempt 并把当前 HANDOFF/result 摘要交给新的 prompt；它不会假装恢复原生会话。

## Governance monitor

Workflow Governance v1 的 monitor 以 60–120 秒区间观察运行态并记录根因。相同根因
连续两次时必须停止自动重试、标记自动继续为 blocked 并升级；不能用缩短轮询或盲目
重发绕过。Herdr wait timeout 也是 inconclusive：先 `agent get` 和 `agent read` 读取
现场，再决定修正、改路由或请求 human gate。确定性判断由
`scripts/workflow_governance.py::monitor_policy` 提供。

## 默认成本边界

统一入口默认最多 3 次尝试，每次有 timeout。这个限制按尝试次数和时间计算，不按某一个供应商的价格单位计算。Codex Goal 另有显式 token budget；wp 不会自动提高预算、延长窗口或创建替代 Goal。

如果用户或项目规则要求不同预算，主控在 HANDOFF 中记录新上限，再显式运行下一次 attempt。没有明确的新授权时，不得把“再试一次”当成无限恢复策略。

## 用户输入

执行者不等待交互式用户输入。能从 HANDOFF、仓库规则、现有约定或最小可逆默认值解决的问题直接解决并记录。只有无法安全推断且确实需要外部信息时才返回 `blocked`。

## 权限请求

runner 默认拒绝扩大权限。依赖、网络和文件范围必须由主控在 HANDOFF 中提前写明。执行者不能切换到 unrestricted/danger-full-access，也不能改全局 Agent 配置。
