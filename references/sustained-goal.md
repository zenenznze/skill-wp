# Codex Goal 长任务

Codex Goal 是 wp 的长任务特色。它适合验收命令明确、可以无人值守运行、预计需要多轮实现和测试修复的任务。

普通任务只需要：

```bash
python3 scripts/run_task.py \
  --repo <execution-root> \
  --task-id <task-id> \
  --agent codex \
  --level hard
```

需要授权长窗口时，再额外给出：

```bash
python3 scripts/run_task.py \
  --repo <execution-root> \
  --task-id <task-id> \
  --agent codex \
  --level hard \
  --timeout-seconds <window-seconds> \
  --token-budget <total-tokens>
```

这里的含义很简单：`timeout-seconds` 是本次运行窗口，`token-budget` 是允许使用的总预算。两者都不会自动增加。窗口结束、额度用尽或 Goal 暂停时，runner 返回 `blocked`，主控检查当前 diff 和 HANDOFF 后，决定是否用 `--continue`/`--resume` 显式恢复同一 thread。

Codex 的一个 turn 结束不代表 Goal 结束，Goal `complete` 也不代表主控已接受。最终仍需要 `verify_result.py`、完整 diff 检查和所有验收命令。

## 执行顺序

1. 启动 `codex app-server --stdio`；
2. 初始化并验证模型和 reasoning effort；
3. 创建或恢复同一个 thread；
4. 设置 Goal 并启动 execution turn；
5. 持续读取 Goal、turn、错误和 token 状态；
6. 处理一次 App Server 重启或一次无事件继续；
7. 写入终态任务文件并交回主控。

Codex runner 不读取凭据，不写 Codex 全局配置，不自动加预算，不自动创建第二个 Goal。

## 预算连续性信号

只有显式的正 `--token-budget` 才启用软阈值。runner 使用原生 Goal 的
`tokensUsed/tokenBudget`，不使用 raw model token totals。Goal 使用量达到或越过
授权预算的 75% 时，runner 通过活动回合的 `turn/steer` 发送一次 checkpoint：要求
执行者把 material conclusions、progress、key files、validation evidence 和
next action 外化到 HANDOFF 或其他持久工作区状态，同时继续当前工作。达到或
越过 90% 时发送一次更强的 convergence 指令：不再开启大型探索分支，完成当前
atomic operation，持久化代码和 HANDOFF，运行必要验证并留下精确 resume point。

同一 usage 更新跨过两个阈值时按 75%、90% 顺序发送；使用量 overshoot 不会
跳过阈值。100% 不由 wp 模拟，仍由 Codex 原生 `budgetLimited` 处理并返回既有
blocked/resume 结果。软信号不会停止 turn、结束 Goal 或增加预算。

阈值 evidence 存在 skill-local `wp-state/.../budget-checkpoints.json`，并由
invocation/thread record 引用。它按 native thread 和授权总预算建立 generation：
App Server 重启或同 thread resume 复用同 generation，新的更大授权预算建立新
generation，同时保留旧证据。线程历史、工作区文件、HANDOFF 和验证记录是可恢复
状态；模型隐藏推理以及 prompt/KV cache 不具备这种持久性，不能作为恢复依据。
