# skill-wp

WP is a standalone **Agent Skill + Pi Plugin** repository for durable, visible delegation.

- `SKILL.md` explains task boundaries, safety, recovery, and acceptance.
- `extensions/wp.ts` registers `/wp` and strict lifecycle, watch, acknowledgement, and cancellation tools.
- `src/core/` is the shared TypeScript state and control plane.
- `bin/wpctl.mjs` is the headless/automation entry.
- `scripts/*.py` remains available during the bounded compatibility window.

## Runtime model

```text
create contract → write HANDOFF/SOP → spawn or reconnect visible Herdr Pi tab
→ wait for result.json while archiving attempt evidence
→ deterministic Git + result review → bounded PASS / RETRY / BLOCKED
→ controller acceptance
```

Herdr lifecycle (`idle`, `done`, process exit) never means semantic success. `run` starts a detached durable supervisor and returns immediately so controllers can poll status while it reconnects, archives attempts, and performs bounded retries. Deterministic Git/result gates remain authoritative; after they pass, Jev supplies a typed completion judgment with configurable probability/confidence thresholds. Missing credentials, low confidence, malformed output, or API failure preserve resources for recovery. Acceptance closes only tabs that carry explicit WP-created ownership; adopted, ambiguous, blocked, and failed resources are retained.

## State

The authoritative default is:

```text
~/.agents/state/wp/
  repos/<repo-id>/tasks/<task-id>/
    HANDOFF.md
    task.json
    result.json
    events.jsonl
    resources.json
    attempts/attempt-NN/{prompt.txt,resources.json,output.json,result.json,review.json}
    reviews/
    checkpoints/
  locks/
```

Set `WP_STATE_DIR` to override it. Target repositories receive no `.agent/` or WP process files. Checkout-local `wp-state/` is legacy migration input only.

Preview and apply migration without deleting the source:

```bash
wpctl migrate --legacy-root /path/to/skill-wp/wp-state
wpctl migrate --legacy-root /path/to/skill-wp/wp-state --apply
```

## Durable-supervisor validation

The supervisor is validated only through its real Pi + WP + Herdr + Git sandbox:

```bash
npm install
npm run e2e:supervisor
```

The E2E compiles as part of the scenario; do not substitute compilation-only, unit, static-analysis, or smoke commands for supervisor acceptance.

## Expose to Pi

Build once, then expose exactly one source entry. Prefer an ASM-managed single-file link into Pi's extension discovery directory. On Windows hosts where file symlinks require unavailable elevation, install the same checkout file as a local single-extension source:

```bash
pi install C:/path/to/skill-wp/extensions/wp.ts
```

This local-path entry executes the checkout directly; it does not copy source or create an aggregate package. Do not register WP through PER as well. After reconciliation, run `/reload` in Pi. The Plugin provides:

- `/wp`
- `wp_task_create`
- `wp_task_status`
- `wp_task_run`
- `wp_task_resume`
- `wp_task_review`
- `wp_task_adopt`
- `wp_task_accept`
- `wp_task_watch` (bind a CLI-created task to this session)
- `wp_task_ack` (acknowledge notification IDs, not acceptance)
- `wp_task_cancel` (preview/confirmed safe retirement, not acceptance)

## wpctl

```bash
npm run build
node bin/wpctl.mjs init --repo <root> --task-id <yyyymmdd-slug> \
  --goal "observable outcome" --write-scope src/a.ts,tests/a.test.ts \
  --acceptance "tests pass|diff is scoped"
node bin/wpctl.mjs status --repo <root> --task-id <id>
node bin/wpctl.mjs run --repo <root> --task-id <id> --controller-tab-id <workspace:tab>
node bin/wpctl.mjs check --repo <root> --task-id <id>
node bin/wpctl.mjs accept --repo <root> --task-id <id>
node bin/wpctl.mjs cancel --repo <root> --task-id <id>           # preview
node bin/wpctl.mjs cancel --repo <root> --task-id <id> --confirm # authorized retirement
```

`run` requires `HERDR_ENV=1`. It starts a detached supervisor, creates one background tab/pane or reconnects to persisted resources, archives attempt evidence, reviews actual Git changes, and performs bounded retry. Poll with `status`; a returned `started` receipt is not task completion. `accept` requires both the current deterministic PASS and a confident Jev `complete` judgment, then closes only tabs proven to have been created by that WP task. Cleanup failure is reported and preserves the resource; WP never auto-closes adopted tabs or workspaces.

## Native controller updates and retirement

Plugin run/resume binds the task to the current Pi session. A session-scoped WP watcher
reads durable event sequences and queues native `wp-update` follow-up messages; it does
not require external `until`. Bind headless-created tasks with `wp_task_watch`.
Delivered IDs are recovered from the active session branch across reload, and
`wp_task_ack` persists consumption explicitly. A crash before a queued message is
persisted can replay that same ID: consumers must treat IDs idempotently, not assume
exactly-once transport. Shutdown stops the watcher; reopening the bound session
recovers pending events. A running headless CLI alone cannot wake an unloaded Plugin.

Updates distinguish resource start, result availability, deterministic review,
semantic judgment, acceptance, timeout, errors and cleanup. Result files and
Herdr idle/done never authorize acceptance. The run receipt reports whether the
supervisor environment contains `TYPESAFE_API_KEY`; provide it through the approved
global environment before launching the controller. No key is copied into state.
A missing key remains a real acceptance blocker, not a bypass opportunity.

Explicit cancellation is independent of acceptance and requires confirmation.
It refuses a live supervisor, adopted/unknown resources, active agents, missing
result/output evidence, and any undelivered repository delta or claimed changes.
It checks live single-pane topology and agent identity, closes only the owned tab,
then reads back topology. Successful retirement records `failed` plus a
`cancelled` event; it never claims semantic acceptance. This deliberately
conservative route does not discard writer changes or forcibly stop working agents.

## Direct Herdr gate

The Plugin blocks common direct scheduling chains (`herdr tab create`, `herdr agent start`, `herdr agent prompt`) issued through Pi's bash tool. Read-only inspection, focus/read/snapshot, Herdr diagnostics, and explicit time-limited `/wp` rescue mode remain available.

## Compatibility

The Python task/graph/supervisor commands remain callable with their existing checkout-local `wp-state/` contract during the migration window. New Plugin and `wpctl` tasks use `WP_STATE_DIR` or `~/.agents/state/wp`. Migrate legacy state explicitly, and move new integrations to the Plugin or `wpctl`; two independent scheduling kernels will not be maintained indefinitely.
