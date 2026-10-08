# 独立验收清单

主控 Agent 在接受任何终态前执行一次独立、只读复核。复核者不得只看执行者的最终消息或 result summary。

每次 review 返回：

```text
VERDICT: PASS | NEEDS_CHANGES | BLOCKED
BLOCKING: <required fixes, or none>
SUGGESTED: <non-blocking improvements, or none>
EVIDENCE: <files, lines, commands, logs, or URLs checked>
```

检查顺序：

1. 目标和验收标准是否全部覆盖；
2. HANDOFF 是否足够且只包含决策相关事实；
3. 完整 diff 是否只改了目标范围；
4. 空值、错误、重复、超时、恢复和兼容性是否正确；
5. 权限、凭据、路径和用户数据边界是否保持；
6. 并发、锁、回滚和失败后的可恢复状态是否合理；
7. 是否有不必要的复杂度、死代码或文档与实现不一致；
8. 测试、schema、CLI help 和示例是否同步。

主控必须独立重新运行 HANDOFF 中的每一条验收命令。验证脚本通过只说明任务文件符合协议，不能代替测试或 diff review。

对于高风险任务，优先使用与 writer 不同供应商的 reviewer；没有可用 reviewer 时，主控在新上下文中自行复核，并明确记录独立性降低。
