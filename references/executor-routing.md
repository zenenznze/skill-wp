# 执行者路由

当前调用技能的 Agent 负责路由。`auto` 不是“固定委派给某一个供应商”，而是先判断任务难度，再根据客户端能力、实时探测和本地偏好选择一个有界 Agent。

## 能力级别

| 级别 | 判断 |
|---|---|
| `fast` | 时间优先、边界清楚、实现和验收都短 |
| `balanced` | 性能和价格平衡，默认值 |
| `hard` | 高难度、长时间、跨模块、迁移或预计多轮修复 |

`frontier` 只作为旧参数映射到 `hard`。显式 `--level` 优先于自动推导；否则 `--time-sensitive` 推导 `fast`，完整 HANDOFF 加 `--long-task --no-time-pressure` 推导 `hard`，其余为 `balanced`。

## 客户端选择

普通 Agent client 包括 Claude Code、Pi、Grok 和 Kimi。Codex 也是客户端，但它额外提供原生 Goal/thread transport，因此 `hard` 任务优先使用 Codex Goal。

默认顺序：

```text
balanced / fast: Claude Code -> Pi -> Grok -> Kimi -> Codex Goal
hard:            Codex Goal -> Claude Code -> Pi -> Grok -> Kimi
research:        Grok -> Claude Code -> Pi -> Kimi -> Codex Goal
```

`--information-retrieval` 或 `--research` 表示任务需要实时搜索、来源检索、外部生态信息或资料对比。这个信号把 Grok 放到首位，因为 Grok 的搜索和检索生态适合这类任务；它不会自动改变普通编码任务的路由。

`--agent` 或旧别名 `--executor` 显式指定客户端后，仍会保留任务级别和健康检查，但不再走自动顺序。

## 实时探测

```bash
python3 scripts/discover_executors.py --probe --out wp-state/roster.json
```

探测记录已安装客户端、版本、transport 能力和模型目录。`degraded` 客户端不能被选择；`unknown` 只能在主控确认后使用。roster 是一次运行快照，不是用户配置。

## 模型

模型解析顺序统一为：

1. 显式模型参数；
2. 客户端实时模型目录；
3. wp 对级别的默认模型；
4. 无法确认时记录 `inherited`，不伪造模型名。

当前默认映射：

- Claude Code：`fast → haiku`，`balanced → sonnet`，`hard → opus`；
- Codex Goal：通常 `hard / 规划 → gpt-5.6-sol`，`fast / 日常执行 → gpt-5.6-luna`，默认 reasoning effort `xhigh`；但当前控制端 `PI_MODEL` 是 Sol 且未显式传入 `--model` 时，writer 在所有级别默认使用 `gpt-5.6-luna` + `xhigh`，避免 Sol 同时承担主控与有界执行；显式模型优先。所有模型仍由原生 `model/list` 和 reasoning effort 校验；
- Kimi：`kimi-code/k3`；
- Grok：从 `grok models` 优先选择 `grok-4.6`、`grok-4.5` 或目录中更旧的兼容模型；
- Pi：模型继承 Pi 自己的配置，wp 不读取或改写 Pi 的全局状态。

## 自定义层

路由偏好写在技能目录的 `wp-custom/agents.md`。只有当前主控 Agent 明确读取并采用的内容才可以影响任务；wp 不自动读取历史画像、设备状态或隐藏 registry。
