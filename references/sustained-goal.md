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
