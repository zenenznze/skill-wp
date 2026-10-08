# wp 图编排

## 定位

wp 的运行时入口仍然是一次一个任务：`init_task.py` → `run_task.py` → `verify_result.py`。图编排层不改单任务语义，它回答的是“哪些任务现在就绪、可以同时启动”，并让一次多任务委派拥有可恢复的调度状态。

图状态只存在于技能目录的 `wp-state/repos/<repo-id>/graphs/<graph-id>/`，不写入目标仓库。一个图包含任务节点和 artifact 节点，由 `graph.json` 描述；`run_graph.py` 把每个任务节点当作同一个 execution root 里的有界 `run_task.py` 子进程来调度。

## 图模型

同一个 `graph.json` 同时是三种视图：调度图、上下文交接图、理解图。

### 三种边关系

| 关系 | 形状 | 回答的问题 |
|---|---|---|
| `depends_on` | task → task | 调度：这个任务什么时候可以启动？ |
| `produces` / `consumes` | task → artifact / artifact → task | 上下文交接：上游留下了什么？下游要加载什么？ |
| `relates` | 任意 → 任意 | 理解：这些节点为什么相关？ |

- `depends_on` 是唯一的调度依赖。`ready_tasks` 只关心“一个任务的全部有效依赖是否都是 success”。
- `produces` / `consumes` 描述 artifact 的生产和消费。下游任务通过 `consumes` 拿到上游产出物，`effective_dependencies` 会把 `consumes` 解析到产出该 artifact 的生产任务，因此消费 artifact 也会形成调度顺序。
- `relates` 不参与调度，只用来记录节点之间的非时序关联，帮助理解和导航图。

### 无依赖理由，就没有边

每条边必须有非占位 `reason`：

- strip 后非空；
- 长度至少 12 个字符；
- 不能匹配占位模式 `todo`、`tbd`、`tbc`、`unknown`、`n/a`、`none`、`xxx`、`.`（大小写不敏感）。

校验由 `graph_lib.validate_graph` 强制执行，`init_graph.py --graph-json` 导入时和 `verify_graph.py` 都会运行它。意图是禁止“先做 A 再做 B”这类伪时序叙述边；只有真实依赖才配拥有边，没有依赖理由就没有边。

## 就绪集合

- `ready_tasks(graph)`：状态为 `pending` 且全部有效依赖都是 `success` 的任务节点，按节点 id 升序，确定性输出。
- 有效依赖（`effective_dependencies`）= 直接 `depends_on` 源 + 该任务 `consumes` 的每个 artifact 的全部 `produces` 生产者。

## 写冲突

每个任务节点可以声明 `writes`：仓库相对 glob 列表。批处理用保守检测避免两个任务同时写可能重叠的区域：

- 未声明 `writes` 或写集为空 = 写范围未知 = 与所有其他节点冲突。这类节点只能独占一个批次。
- 否则把每个 glob 截断到第一个 `*` 或 `?`，得到静态前缀；两个写集冲突当且仅当某个前缀是另一个的完整路径段前缀，或二者相等。
- 这是过近似：宁可串行，也不猜重叠。

例如 `src/**` 与 `src/auth/**` 冲突（串行），`src/a/**` 与 `src/b/**` 不冲突（可并行）。

## 批处理

`plan_batch(ready, graph, max_parallel)`：贪心确定性。就绪节点按 id 升序逐个尝试加入当前批；与批内任何节点写冲突则跳过；达到 `max_parallel` 停止。因此一次调度循环可能跑多个批次，每批内部是互不冲突的任务。

## 调度循环（run_graph）

Atomic Work Contract 预检发生在 dry-run 和节点启动之前：每个任务节点必须有
有效的 v2 HANDOFF，且 `write_scope` 必须与节点 `writes` 完全一致；只读任务
也必须声明 `writes: []`。一次预检会收集全部缺失、契约错误和写集不一致，任一
错误都会阻止整个图。无版本 HANDOFF 默认也会作为 legacy 错误逐节点汇总；只有
`run_graph.py --allow-legacy-task-package` 才允许兼容路径，并把同名旗标转发给
每个 legacy 子任务。兼容路径仍不声称满足 v2 契约。完整契约与层级说明见
[`atomic-work.md`](atomic-work.md)。

1. 加载 `graph.json` 并 `validate_graph`；校验失败逐条打印并退出 2。
2. 加锁：创建 `<graph_dir>/run.lock`（`O_CREAT | O_EXCL`），内容为 `pid` 和时间戳。锁已存在时拒绝启动并退出 2；正常退出在 `finally` 删除锁。进程崩溃后留下的 stale 锁是有意的恢复屏障：确认没有其他 run_graph 在跑后，删除锁即可恢复。
3. 预检：每个任务节点必须有已初始化且契约有效的任务包。缺失、不完整或写集
   不一致时一次列出全部错误并退出 2，绝不自动初始化或启动部分节点。
4. 循环：
   - 就绪集 = `ready_tasks` + 尚有尝试余量的 `failed` 节点（`attempts < --max-node-attempts` 且有效依赖全部 success）。
   - 没有就绪节点时，把图状态设为 `graph_status(graph)`，持久化，结束。
   - `batch = plan_batch(ready, graph, --max-parallel)`。
   - 批内每个节点：状态置 `running`，`attempts += 1`，持久化，然后以 `subprocess` 启动对应任务。
   - 每 5 秒轮询批次；节点退出后读取其 `result.json` 并运行既有的终态校验；校验通过则节点状态取 `result` 的 status，否则置 `failed` 并把协议错误写入该节点的 `last_error`。
   - 每次节点状态变化都原子写回 `graph.json`，不只在批次结束时写；每个节点完成后向 `GRAPH_HANDOFF.md` 的 `# Execution Progress` 追加一行（时间戳、节点 id、task_id、attempt、退出码、终态）。

### 节点启动命令

每个节点都是同一 execution root 内的有界 `run_task.py` 子进程：

```bash
python3 scripts/run_task.py \
  --repo <execution-root> \
  --task-id <node.task_id> \
  --agent <--agent> \
  --level <--level> \
  --attempt <当前尝试编号> \
  --max-attempts <--max-node-attempts> \
  [--timeout-seconds N] [--roster <roster-file>]
```

- `--max-attempts` 固定等于图级 `--max-node-attempts`，保证 attempt 编号落在 `run_task.py` 的接受范围。
- `--timeout-seconds` 只在显式给出时传递；未给出时由 `run_task.py` 使用客户端默认超时。
- 图节点不改变路由：客户端选择、roster、timeout 语义都和单任务路径一致。

### 恢复语义

同一 `--graph-id` 再次运行 `run_graph.py` 即可恢复，不需要重建状态：

- `success` 节点被跳过，不再启动；
- `pending` 节点从 `attempt = attempts + 1` 开始启动；
- `failed` 节点只在 `attempts < --max-node-attempts` 时重试，尝试耗尽后保持 `failed`。

每次节点转变都原子持久化，所以中断只会留下“真实但未完成”的图，不会留下假终态。

## 终态和退出码

图级终态由 `graph_status(graph)` 推导：

| 状态 | 条件 |
|---|---|
| `success` | 至少一个任务节点且全部任务节点为 `success` |
| `failed` | 任一任务节点 `failed` 且没有就绪节点剩余 |
| `blocked` | 任一任务节点 `blocked` 且没有 failed/就绪节点剩余 |
| `running` | 任一节点 `running` |
| `pending` | 其他 |

退出码：

- `init_graph.py`：0 成功；2 参数、导入或校验错误。
- `run_graph.py`：0 图 `success`；3 图 `blocked`；2 图 `failed` 或校验/预检/锁错误。
- `verify_graph.py`：0 协议有效；2 任何协议错误；3 带 `--require-success` 且图状态不是 `success`。

## 编排者拥有的 worktree 集成

图节点是同一个 execution root 内的有界实现者，`run_graph.py` 不做任何 Git 变更：不 push、不 merge、不创建或切换分支，也不改目标仓库的状态目录。

并行代码编辑的隔离由编排者拥有：

- 编排者建立 Git worktree 或分支，让需要并行、互不冲突的图节点各自在独立 checkout 上工作；
- 节点完成并通过 `verify_graph.py` 后，编排者审查每个节点的 diff、独立重跑验收命令，再把多个节点成果整合进主 checkout；
- 执行者永不 push/merge；`GRAPH_HANDOFF.md` 的 `# Integration And Acceptance` 是编排者的合并和验收清单。

没有任何脚本自动创建 worktree；这是编排者的工作方式，不是 `run_graph.py` 的功能。

## 示例图

后端重构图：5 个任务节点 + 3 个 artifact 节点，展示扇出（fan-out）、屏障（barrier）和扇入（fan-in）。`repo_id`、`created_at`、`updated_at` 由 `init_graph.py --graph-json` 在导入时补齐，作者可以省略。

```json
{
  "graph_id": "20260820-backend-refactor",
  "goal": "在既有后端约束下完成认证设计和 API 契约，然后实现并集成。",
  "status": "pending",
  "nodes": [
    {"id": "backend-research", "kind": "task", "task_id": "20260820-backend-research",
     "goal": "调研现有后端约束", "status": "pending", "writes": ["docs/backend-constraints.md"]},
    {"id": "constraints-doc", "kind": "artifact", "path": "docs/backend-constraints.md", "status": "pending"},
    {"id": "auth-design", "kind": "task", "task_id": "20260820-auth-design",
     "goal": "设计认证方案", "status": "pending", "writes": ["docs/auth-design.md"]},
    {"id": "auth-spec", "kind": "artifact", "path": "docs/auth-design.md", "status": "pending"},
    {"id": "api-contract", "kind": "task", "task_id": "20260820-api-contract",
     "goal": "确定 API 契约", "status": "pending", "writes": ["docs/api-contract.md"]},
    {"id": "api-spec", "kind": "artifact", "path": "docs/api-contract.md", "status": "pending"},
    {"id": "backend-impl", "kind": "task", "task_id": "20260820-backend-impl",
     "goal": "按设计和契约实现后端", "status": "pending", "writes": ["src/backend/**"]},
    {"id": "integration", "kind": "task", "task_id": "20260820-integration",
     "goal": "集成测试并准备交接", "status": "pending", "writes": ["tests/integration/**", "docs/CHANGELOG.md"]}
  ],
  "edges": [
    {"from": "backend-research", "to": "constraints-doc", "relation": "produces",
     "reason": "调研结论写入约束文档供下游设计参考"},
    {"from": "constraints-doc", "to": "auth-design", "relation": "consumes",
     "reason": "认证设计必须符合既有后端约束"},
    {"from": "constraints-doc", "to": "api-contract", "relation": "consumes",
     "reason": "API 契约必须遵守既有后端约束"},
    {"from": "auth-design", "to": "auth-spec", "relation": "produces",
     "reason": "认证设计稿产出为 spec 文档"},
    {"from": "api-contract", "to": "api-spec", "relation": "produces",
     "reason": "契约定稿产出为 spec 文档"},
    {"from": "auth-spec", "to": "backend-impl", "relation": "consumes",
     "reason": "实现必须按认证 spec 编写"},
    {"from": "api-spec", "to": "backend-impl", "relation": "consumes",
     "reason": "实现必须按 API spec 编写"},
    {"from": "auth-design", "to": "backend-impl", "relation": "depends_on",
     "reason": "实现要等认证设计完成后才动手"},
    {"from": "api-contract", "to": "backend-impl", "relation": "depends_on",
     "reason": "实现要等 API 契约定稿后才动手"},
    {"from": "backend-impl", "to": "integration", "relation": "depends_on",
     "reason": "集成测试要在实现落盘后进行"}
  ]
}
```

调度过程：

| 批次 | 就绪节点 | 说明 |
|---|---|---|
| 1 | `backend-research` | 唯一无依赖节点 |
| 2 | `api-contract`、`auth-design` | 扇出：两个设计任务在约束明确后并行（写集不冲突） |
| 3 | `backend-impl` | 屏障：必须等认证设计和 API 契约都完成 |
| 4 | `integration` | 扇入：最终集成 |

关键路径：`backend-research` → `api-contract` → `backend-impl` → `integration`（两条等长链按节点 id 字典序选择）。

`backend-impl` 的上下文包（`required_context`）会包含传递上游任务 `backend-research`、`auth-design`、`api-contract` 以及它们产出的 artifact 和边，而不是按时间顺序的前驱。
