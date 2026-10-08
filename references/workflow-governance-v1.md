# Workflow Governance v1

Workflow Governance v1 is an additive control-plane contract above wp's
existing Atomic Work Contract, graph, runner, and terminal-result protocols. It
answers who may act, how much approval is required, how work is monitored and
reviewed, and whether deployment is authorized. It does **not** replace or
reinterpret the v2 task-package parser.

A controller may persist a JSON record with
`workflow_governance_version: 1` in skill-local `wp-state/`. Validate the record
with `scripts/workflow_governance.py`. Existing ungoverned v2 task packages and
explicit legacy compatibility remain parseable exactly as documented; adoption
is additive and controllers should migrate new workflows first.

## Four risk levels

Classification is deterministic and conservative:

| Level | Deterministic meaning | Default gate |
|---|---|---|
| `read-only` | No writes, command execution, or side-effect signal. | Controller may proceed within declared read scope. |
| `tiny` | At most two files, bounded and reversible write scope, no command execution or elevated signal. | Controller approval; no human gate required by this module. |
| `medium` | Default for any side effect, execution, uncertainty, or work that does not satisfy `tiny`. | Explicit scoped human approval before implementation. |
| `high-risk` | Production/deployment, destructive or irreversible action, credentials, permissions/security boundary, data migration, or external publication. | Explicit scoped human approval; separate action gates still apply. |

`minimum_risk` may only raise the computed level. Risk signals use a closed
known set; an unknown or misspelled signal, alone or mixed with known signals,
is conservatively classified at least `medium` and is reported as unknown.
A governance task must explicitly provide strictly typed `writes`, `executes`,
`signals`, `file_count`, `bounded_scope`, and `reversible` fields; missing or
coerced values are invalid. A human gate authorizes only the named actions and
scope. Approval of implementation, verification, commit, or push never implies
production deployment.

## Six role slots

| Slot | Authority |
|---|---|
| `orchestrator` | Owns repository understanding, plan, routing, gates, acceptance, and reporting. |
| `sole-writer` | The single Agent allowed to modify the declared repository/config scope. Exactly one is required for writing work. |
| `reviewer` | Independent, read-only review; should be a different vendor when available. |
| `scout` | Read-only bounded research and evidence gathering. |
| `monitor` | Read-only status observation and escalation; normally the orchestrator. |
| `human-approver` | A real human who decides approval. This is not an Agent identity and grants no runner permission. |

Reviewer, scout, and monitor must be read-only. Slot assignment never grants
deployment authority. The human approver must not be represented by an Agent,
model, automated rule, or inferred intent.

## Approval and pending-only signoff

Every approval record names:

- `actor_type: human`;
- exact non-empty `actions` drawn from `implement`, `verify`, `commit`, `push`,
  and `deploy`;
- exact non-empty string `scope` covered by the decision;
- `status: approved` and the human's preserved statement.

The requested action and required scope are validated before set operations.
Nulls, mappings, scalar strings or numbers, empty strings, and empty arrays are
invalid rather than coerced.

Silence, a broad goal, an Agent completion claim, and a prior unrelated approval
are not approval. An Agent may create a signoff record only with `status:
pending`. Only a human can authorize `approved`, `changes_requested`, or
`rejected`; any mechanical recording must preserve the human statement
verbatim. Signoff remains separate from task execution results and production
data.

## Review, verification, and acceptance

These are distinct evidence layers:

1. the executor writes HANDOFF/result terminal artifacts;
2. `verify_result.py` validates protocol shape and consistency;
3. a read-only reviewer inspects requirements, full diff, safety, compatibility,
   tests, and recovery behavior;
4. the controller independently reruns acceptance commands and decides whether
   to accept;
5. human signoff remains `pending` until the human decides.

An executor result, process exit, Goal completion, or Herdr `done`/`idle` state
is never controller acceptance, human signoff, or deployment success.

## Monitor policy

Use a 60–120 second interval for long-running work. Each observation records the
current attempt, evidence, root-cause fingerprint, and retry decision. If the
same root cause occurs twice consecutively, stop automatic retries, mark the
work blocked for automatic continuation, and escalate to the controller or
human gate. A changed hypothesis or route may begin a new bounded attempt; the
policy forbids blind repetition, not evidence-based correction.

This governance policy is stricter than merely detecting unchanged files. The
existing `monitor_task.py` remains compatible as an artifact-growth utility;
controllers apply `monitor_policy()` to retry decisions.

## Independent deployment gate

Deployment is a separate high-risk phase. Its requester uses an exact
allowlist: only the verified `orchestrator` or `controller` identity is valid.
A missing, empty, differently cased, unknown, runner-like, or non-string
requester is rejected. A normal wp runner cannot authorize or perform it.
Passing tests, accepting a diff, approving a commit/push, or asking to “execute
everything” does not imply deployment authorization.

Before deployment, the controller must have all of:

1. explicit human approval containing action `deploy` and the exact target
   scope;
2. successful preflight/configuration checks;
3. a verified backup and restore path;
4. planned post-deploy health checks;
5. planned startup/application log checks;
6. a ready rollback procedure.

Deploy only approved components. On any failure, stop rollout, preserve
failure evidence, execute or report the rollback path, then repeat health and
log checks. Report actual results; never convert a failed deploy or rollback
into success.

## Pi-only Herdr control plane

When Herdr is used for this workflow:

- A clearly named, standalone topic creates its own Space/workspace. An ordinary
  directory task reuses the unique workspace whose normalized `cwd` matches;
  if none exists, create one. Multiple workspaces claiming one cwd are a
  topology blocker that must be organized first.
- Keep exactly one semantic controller tab and one pane per background tab.
  Create tabs with `--no-focus`; do not split panes by default.
- Start every child with `herdr agent start --kind pi`. Use semantic execution
  labels, not numeric or generic `worker`/`reviewer` names.
- Record and report the real `workspace_id`, `tab_id`, `pane_id`, and returned
  Agent ID/name. Never infer IDs from visual position.
- A wait timeout is inconclusive. Run `agent get` and `agent read` before any
  retry; do not resend blindly.
- `done`, `idle`, exit status, and a final message are lifecycle evidence only.
  The controller must independently review and accept the result.
- After acceptance, rename the execution tab `完成-<具体任务>` before closing it.
  Preserve the controller tab and close only accepted, integrated, unblocked
  execution resources.

Use `select_herdr_space()` and `validate_herdr_plan()` for deterministic space
selection and topology checks. Integer topology fields such as
`controller_tabs` and `pane_count` are strict integers: booleans, negatives,
floats, and strings are invalid. Herdr remains the control plane; wp's HANDOFF,
runner, result, review, and acceptance contracts remain the execution evidence.

## Compatibility boundary

Workflow Governance v1 intentionally adds no field to the v2 HANDOFF contract,
`result.schema.json`, graph node schema, or runner CLI. Therefore:

- v2 package parsing and graph scheduling remain unchanged;
- explicit `--allow-legacy-task-package` behavior remains unchanged;
- existing executor results remain valid transport evidence;
- governance records may be introduced gradually under local `wp-state/`;
- a future parser integration requires a new, explicitly versioned migration,
  not silent reinterpretation of v2.

The authoritative deterministic implementation is
`scripts/workflow_governance.py`; focused behavior tests are in
`tests/test_workflow_governance.py`.

## Executable Herdr bridge boundary

`scripts/herdr_pi_control.py` is the single Herdr CLI coupling point. It is an
additive control-plane adapter, not a replacement for the governance validator
and not an extension of the Atomic Work Contract, result parser, graph parser,
or runner. Use `plan` to validate a JSON plan without invoking Herdr, `run` to
create Pi-only execution resources and write a lifecycle-only receipt below
`wp-state/herdr-receipts/`, and `close` only after an independent controller
sends `accepted=true`. Examples:

```bash
python3 scripts/herdr_pi_control.py plan --input @plan.json
HERDR_ENV=1 python3 scripts/herdr_pi_control.py run --input @plan.json --receipt runs/payment.json
python3 scripts/herdr_pi_control.py close --receipt runs/payment.json --accepted
```

`plan` and successful `run` print a JSON object to stdout. Errors print a
structured `{"error": ..., "message": ...}` object to stderr and exit 2;
tracebacks are not part of the CLI contract. A run receipt is a complete,
non-overwritable lifecycle record (`closed: false`) containing only real Herdr
IDs. `done`/`idle` are lifecycle fields, not task results; `unknown`, blocked,
timeout, command failure, and protocol failure remain inconclusive/error
states. `close` reads and validates the named local receipt, re-reads Herdr
topology, requires `accepted=true`, renames the created execution tab to
`完成-...`, closes only that tab, then atomically marks the receipt closed.
It rejects forged, tampered, repeated, controller, user-owned, or symlinked
receipts. Receipt provenance is an HMAC-SHA256 under the private
`wp-state/herdr-receipts/.bridge-key` (0600, owner-checked, no symlink); the
key is never accepted as a receipt path and a public hash is not sufficient.
Receipt reads and close use no-follow file descriptors and an exclusive file
lock. Run journals each real workspace/tab/pane/agent ID as soon as it is
known. A prompt/start/protocol/command failure leaves a signed
`partial: true` lifecycle receipt with the error class/message rather than
leaving untracked resources or claiming success; partial receipts are not
closable automatically. Only an explicit structured timeout is classified as
timeout; an ordinary error mentioning the word “timeout” remains a command
failure. The bridge never declares task success, writes controller acceptance,
deploys, or removes user-owned resources. See `tests/test_herdr_pi_control.py`
for fake-CLI and adversarial contract coverage.
