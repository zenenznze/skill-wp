---
name: wp
description: >-
  Use when an Agent needs to delegate a non-trivial repository implementation,
  coding task, refactor, debugging session, or bounded research pass to another
  Agent with durable handoff state, terminal result validation, and independent
  acceptance; use for cross-file refactors, long Codex Goal tasks, Goal resume,
  Grok information retrieval, source search, and bounded repository research;
  当 Agent 需要委派仓库实现、代码修改、跨文件重构、长任务恢复、Grok 资料检索，
  并需要可恢复交接、结果校验和独立验收时使用。
---

# wp：仓库委派

`wp` 把一个非平凡仓库目标交给另一个有界 Agent 执行，并保留可恢复的交接、执行记录和终态结果。
它适合需要多步实现、跨文件修改、长时间运行或独立复核的任务。

## 角色

当前调用本技能的 Agent 是主控 Agent，负责：

- 理解目标仓库和项目规则；
- 写目标、边界和验收标准；
- 选择客户端和能力级别；
- 处理权限、阻塞、重试和恢复；
- 检查完整 diff、重新运行验收并决定是否接受；
- 按项目规则完成 Git 交付。

被委派 Agent 只负责指定 execution root 中的有界实现和验证。它不能推送、发布、部署、读取凭据、修改用户全局配置或扩大权限。用户目标和可观察验收高于任何建议实现。

## 客户端和路由

| 客户端 | 定位 | 适合场景 |
|---|---|---|
| Claude Code | 普通 Agent client | 快速、局部、常规仓库修改 |
| Pi | 普通 Agent client | 终端环境、已有 Pi 工作流或需要同类 Agent 执行 |
| Grok | 普通 Agent client | 明确需要实时信息搜索、资料检索、外部生态信息时优先 |
| Kimi | 普通 Agent client | 一次性、有界实现、文档或研究任务 |
| Codex Goal | 普通客户端中的特殊 transport | 高难度、长时间、需要原生 Goal 和同一 thread 恢复的任务 |

能力级别只有三档：

- `fast`：时间优先，任务边界清楚，接受较低的模型成本和上下文投入；
- `balanced`：性能和价格之间的平衡，默认级别；
- `hard`：高难度或长任务，预期更强推理、较多实现-测试-修复循环；旧名 `frontier` 仍兼容，但不再作为文档用语。

`auto` 先决定级别，再选择健康且能执行该级别的客户端。普通默认顺序是 Claude Code、Pi、Grok、Kimi、Codex；`hard` 优先 Codex Goal；带 `--information-retrieval` 时 Grok 优先。显式 `--agent` 会覆盖自动选择。

## 本地层和自定义

所有 wp 自己产生或维护的内容都在本技能目录内：

- `wp-custom/`：用户自定义配置和想法。只有主控显式读取并采用的内容才进入任务上下文；wp 不自动扫描隐藏文件。
- `wp-state/`：任务 HANDOFF、result、attempt、roster 和运行日志。它按目标仓库的稳定本地 ID 隔离，默认不进入 Git。

目标仓库只作为执行根目录，接收被委派 Agent 的代码改动；wp 不再在那里创建 `.agent/`、任务记录或设备状态。

## 工作流程

1. 读取目标仓库的规则、Git 状态、实现、测试和交付边界。
2. 把用户目标整理成可观察结果、非目标和验收命令。
3. 在 `wp-state/` 创建任务包，HANDOFF 是唯一完整的人类可读状态源。
4. 实时探测可用客户端，按级别、任务类型和 `wp-custom/agents.md` 路由。
5. 启动一个有界 Agent。建议实现只是当前方向，执行者可以在边界内采用更合适的仓库原生方案。
6. 运行期间观察日志和状态，处理停滞、重复失败、额度限制和可恢复 Goal 状态。
7. runner 退出后先运行 `verify_result.py`，再由主控检查完整 diff 并重新运行所有验收命令。
8. 主控独立复核后接受、修复、阻塞或失败；不把 Agent 的最终文字、进程退出码或自报测试当作完成证据。

## 退出条件

一次任务只在以下三种终态之一结束：

- `success`：实现完成，所有验收命令通过，主控已检查 diff 并接受；
- `blocked`：必须等待明确的外部动作，例如服务恢复、额度恢复或用户提供不可发现的信息；
- `failed`：实现或验证仍错误、传输不可恢复、协议不完整，或有界尝试已耗尽。

默认最多 `3` 次尝试：首次执行加最多两次聚焦修复。每次有独立的 `--timeout-seconds`；相同根因连续出现两次就停止继续重试并改路由。Codex Goal 的 token budget 必须显式给出，绝不自动增加。普通中断按 `failed` 处理；Codex 的明确 usage/budget/timebox 限制可以按协议进入 `blocked`，之后由主控显式继续。

## 命令

先初始化：

```bash
python3 scripts/init_task.py \
  --repo <execution-root> \
  --task-id <yyyymmdd-slug> \
  --goal "<one-paragraph observable outcome>" \
  --agent auto \
  --level balanced
```

再运行：

```bash
python3 scripts/run_task.py \
  --repo <execution-root> \
  --task-id <yyyymmdd-slug> \
  --agent auto \
  --roster <roster-file>
```

终态验证：

```bash
python3 scripts/verify_result.py \
  --repo <execution-root> \
  --task-id <yyyymmdd-slug> \
  --require-success
```

`init_task.py` 只创建技能目录内的任务包，不启动客户端，也不修改目标仓库。`run_task.py` 读取 HANDOFF，探测并启动 Agent。`verify_result.py` 读取同一个 skill-local state，检查 task ID、终态、必填字段、HANDOFF/result 一致性、阻塞字段、验证记录和 sentinel；它不代替主控运行测试、检查 diff 或做独立 review。

## 安全边界

- 不把凭据、token、cookie、原始认证日志或用户全局配置写入任务包。
- 不让执行者 push、publish、deploy 或进行目标外不可逆操作。
- 不用聊天记录替代 HANDOFF，也不把整仓源码或原始日志复制进 HANDOFF。
- 不使用安装、同步和公开发布流程；这些属于 `agent-skill-sync` 或项目规则。

## 相关文件

- `README.md`：面向使用者的完整命令和参数说明；
- `references/protocol.md`：状态、职责和终态协议；
- `references/executor-routing.md`：级别、客户端和 Grok 检索路由；
- `references/blocker-policy.md`：停滞、限制、重试和恢复；
- `references/review-checklist.md`：主控独立验收清单；
- `scripts/init_task.py`、`scripts/run_task.py`、`scripts/verify_result.py`：确定性入口。
