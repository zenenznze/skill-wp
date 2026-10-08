# wp：仓库实现委派

`wp` 是一个面向 Agent 的目标驱动委派技能。当前调用技能的 Agent 是主控 Agent；它理解仓库、组织任务、选择客户端、控制范围、验收结果并负责交付。另一个 Agent 只在指定 execution root 中完成有界实现。

Codex Goal 是 wp 的特色能力：它使用 Codex 原生 Goal/thread，可以在长任务或受限窗口后继续同一个 Goal。Claude Code、Grok、Kimi 和 Pi 都是普通 Agent client；它们没有被包装成“主控”，也不拥有最终验收权。

## 先看这里：自定义和本地状态

用户自己的配置和想法只放在技能目录：

```text
wp-custom/
├── agents.md          # 用户自定义路由、客户端偏好和本地约束
└── ideas/              # 想法和待验证方案
```

`wp-custom/` 被 Git 忽略，不会进入公共发布。仓库里提供的 `wp-custom/README.md` 是说明，不是用户配置本身。

wp 的任务状态也只写技能目录的 `wp-state/`：

```text
wp-state/
└── repos/<repo-id>/
    ├── tasks/<task-id>/       # HANDOFF、prompt、result、revision
    └── runs/<task-id>/        # invocation、Goal/thread、客户端日志
```

`repo-id` 是目标仓库路径的本地稳定标识，不把目标仓库绝对路径写进任务记录。目标 execution root 仍然会被执行 Agent 修改，因为那是用户要求交付的代码位置；除此之外，wp 不在目标仓库创建 `.agent/` 或其他持久状态。

## 路由规则

能力级别：

| 级别 | 含义 | 默认使用场景 |
|---|---|---|
| `fast` | 时间优先 | 小范围、边界清楚、需要快速返回 |
| `balanced` | 性能和价格平衡 | 默认，常规仓库实现 |
| `hard` | 高难度 | 跨模块、迁移、长任务、复杂调试 |

旧参数 `frontier` 仍映射为 `hard`，只是兼容别名。

客户端路由：

| 条件 | 优先顺序 |
|---|---|
| 默认 `balanced` | Claude Code → Pi → Grok → Kimi → Codex |
| `fast` | Claude Code → Pi → Grok → Kimi → Codex |
| `hard` | Codex Goal → Claude Code → Pi → Grok → Kimi |
| 需要信息搜索或资料检索 | Grok → Claude Code → Pi → Kimi → Codex |
| 显式 `--agent` | 直接使用指定客户端 |

Grok 的检索优先级只在明确使用 `--information-retrieval`/`--research` 时生效，不会把普通编码任务盲目改成检索任务。

## 标准流程

### 1. 初始化任务

```bash
python3 scripts/init_task.py \
  --repo <execution-root> \
  --task-id <yyyymmdd-slug> \
  --goal "<one-paragraph observable outcome>" \
  --agent auto \
  --level balanced
```

执行后会：

1. 确认 `--repo` 是 Git 根目录；
2. 为目标仓库计算本地 `repo-id`；
3. 在 `wp-state/repos/<repo-id>/tasks/<task-id>/` 创建 HANDOFF、执行 prompt、Codex Goal 目标、Claude 权限配置和 result schema；
4. 在 `wp-state/repos/<repo-id>/runs/<task-id>/` 创建运行目录；
5. 写入目标、主控 Agent、初始客户端和级别元数据。

它不会启动 Agent，不会修改目标仓库，也不会替用户填写 HANDOFF 占位内容。初始化完成后，主控 Agent 需要补齐相关文件、约束、验收标准和命令。

初始化参数：

| 参数 | 作用 |
|---|---|
| `--repo` | 要被修改的 Git 根目录，默认当前目录 |
| `--task-id` | `yyyymmdd-lowercase-slug` 格式的任务 ID |
| `--goal` | 一段可观察目标，不写实现步骤 |
| `--agent` | `auto`、`claude`、`codex`、`grok`、`kimi` 或 `pi`；`--executor` 是兼容别名 |
| `--level` | `fast`、`balanced` 或 `hard`；`--capability`/`--tier` 是兼容别名 |
| `--codex-model` | Codex Goal 初始模型覆盖 |
| `--codex-reasoning-effort` | Codex Goal 初始推理强度覆盖 |
| `--kimi-model` | Kimi 初始模型覆盖 |
| `--controller-model` | 主控 Agent 的记录模型名，默认读取 `PI_MODEL`；`--planner-model` 是兼容别名 |
| `--controller-effort` | 主控 Agent 的记录推理强度，默认读取 `PI_REASONING_LEVEL`；`--planner-effort` 是兼容别名 |

### 2. 探测客户端

```bash
python3 scripts/discover_executors.py --probe --out <roster-file>
```

它检查当前机器上已安装的 Claude Code、Codex、Grok、Kimi、Pi，记录版本、可用性、transport 能力和可发现模型。`roster-file` 是一次运行的快照，放在技能目录的 `wp-state/` 中，不是长期配置。

### 3. 启动任务

```bash
python3 scripts/run_task.py \
  --repo <execution-root> \
  --task-id <yyyymmdd-slug> \
  --agent auto \
  --level balanced \
  --roster <roster-file>
```

通用参数：

| 参数 | 作用 |
|---|---|
| `--repo` | execution root |
| `--task-id` | 已初始化任务 ID |
| `--agent` | 选择客户端；`--executor` 为兼容别名 |
| `--level` | `fast`、`balanced`、`hard`；旧 `frontier` 映射为 `hard` |
| `--attempt` | 当前尝试编号，从 1 开始 |
| `--max-attempts` | 最大尝试次数，默认 3 |
| `--timeout-seconds` | 当前尝试的时间上限；不同客户端都使用它作为统一外层限制 |
| `--continue` | 继续当前客户端支持的上一次任务；`--resume` 为兼容别名 |
| `--revision` | 指向当前 revision note，供修复尝试读取 |
| `--roster` | 实时客户端探测快照；健康状态为 degraded 的客户端不会启动 |
| `--information-retrieval` | 把 Grok 放在自动路由第一位 |
| `--research` | 上一参数的简写别名 |
| `--time-sensitive` | 未显式指定级别时选择 `fast` |
| `--long-task` + `--no-time-pressure` | 未显式指定级别且 HANDOFF 完整时选择 `hard` |
| `--token-budget` | Codex Goal 的明确 token 总预算，不会自动增加 |

客户端公共覆盖参数：

| 参数 | 作用 |
|---|---|
| `--model` | Codex 的模型名；显式值优先于自动解析 |
| `--effort` | Claude 的 `high`/`max` |
| `--max-turns` | Claude 的最大回合数 |
| `--claude-bin`/`--codex-bin`/`--grok-bin`/`--kimi-bin`/`--pi-bin` | 覆盖对应客户端可执行命令 |

客户端特殊参数只在确实需要时使用：

- Codex Goal：`--reasoning-effort`、`--idle-timeout-seconds`、`--request-timeout-seconds`、`--token-budget`；
- Claude Code：`--claude-model`、`--permission-mode`、`--isolated`、`--max-budget-usd`；
- Grok：`--grok-model`、`--grok-effort`、`--grok-max-turns`；
- Kimi：`--kimi-model`；
- Pi：`--pi-model`。

### 4. 验证终态

```bash
python3 scripts/verify_result.py \
  --repo <execution-root> \
  --task-id <yyyymmdd-slug> \
  --require-success
```

脚本的原理是机械核对：

1. 根据 `repo` 和 `task-id` 定位技能目录中的同一任务状态；
2. 读取 `result.json`，检查 task ID、终态、摘要、changed files、validation 和 blocker 字段；
3. 检查 `handoff_path` 确实指向 `wp-state/`；
4. 读取 HANDOFF，检查它的 status 与 result 一致；
5. 确认 runner 已移除 `runner_sentinel`；
6. `--require-success` 额外要求 status 为 `success`。

它证明的是“终态文件符合协议”，不是“代码一定正确”。主控仍必须检查完整 Git diff，并重新运行 HANDOFF 中的每一条验收命令。

## 退出和重试

- `success`：实现、验证和主控验收全部完成；
- `blocked`：存在明确外部阻塞，HANDOFF 和 result 必须给出证据、解除动作和恢复位置；
- `failed`：实现仍错误、传输不可恢复、协议损坏或尝试次数耗尽。

默认最多 3 次尝试。每次都有自己的 timeout；相同根因连续两次出现就不再机械重试。Codex Goal 的 Goal `complete` 也只是候选完成，仍需主控验收。Codex 的 usage/budget/timebox 限制可以产生可恢复 `blocked`，继续时必须由主控显式给出新的窗口或预算。

## 边界

安装、技能仓库 checkout、发现链接和跨设备同步属于 `agent-skill-sync`，不属于 wp。项目的 Gitea 提交和推送规则、公开 tracked set 与发布策略属于项目级 `AGENTS.md`，不在 README 中重复维护。

wp 不会自动 cleanup、reset、重新初始化或删除 `wp-state/`、`wp-custom/`。公开内容以 Git tracked set 为准，项目规则负责说明交付策略。

## 许可

自有代码使用 [MIT License](LICENSE)；改编自 agent-sop 的文件保留其原始版权及 MIT 声明，详见 [NOTICE.md](NOTICE.md)。

## 公开历史说明

经作者授权，公开历史已重建为不含旧本机路径的干净首次提交。原公开版本的当前实现予以保留，仅补齐许可证、忽略规则和脱敏测试示例；没有将其他版本的 WP 实现覆盖进来。旧 checkout 需要重新克隆，勿将旧历史合并推回公开仓库。重建远端可达历史不能保证 GitHub 缓存、旧提交链接或他人克隆已经删除。
