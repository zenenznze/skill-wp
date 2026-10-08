# Jev Supervisor Delivery State

Date: 2026-09-25

## Delivered behavior

- Deterministic Git, result, validation, write-scope, retry, and acceptance gates remain authoritative.
- Jev (`jev-latest`) runs only after deterministic PASS and returns a typed `complete`, `retry`, or `blocked` disposition.
- Missing credentials, transport or parse failures, and values below the configured confidence/probability thresholds fail safe to manual review and preserve Herdr resources.
- Semantic evidence is persisted as `semantic.json`; credentials are read only from `TYPESAFE_API_KEY` and are not persisted.
- Supervisor run/resume remains detached and observable through durable task state.
- Acceptance requires both the current deterministic PASS and a confident semantic `complete` result.
- Cleanup considers only resources explicitly marked `created_by_wp: true`, belonging to the current task, and not adopted. Identity/topology must still be reachable immediately before close.
- Adopted, ambiguous, blocked, failed, low-confidence, and cleanup-error resources are preserved. Workspaces are not automatically closed.
- Attempt resource records retain lifecycle and cleanup evidence across bounded retries.

## Verification state

Passed:

- `npm test`: 17 tests passed; the explicitly environment-gated live TypeSafe smoke test was skipped.
- TypeScript build through `npm test`.
- JavaScript syntax check for the E2E script.
- `python -m py_compile scripts/*.py`.
- `python scripts/verify_result.py --help`.
- `python scripts/verify_graph.py --help`.
- `git diff --check` (line-ending warnings only).

Real `npm run e2e:supervisor` was executed repeatedly and advanced far enough to prove the configured `walker` account works when normal Pi extensions are loaded. Earlier `invalidated oauth token` output was caused by the E2E harness using `--no-extensions`, which suppressed the Accounts extension; the harness no longer does that.

The final full E2E did not reach an all-green result before closeout:

- Initial CPA attempts failed upstream with TLS handshake EOF.
- After loading the normal extension set, the write/retry/semantic/accept/cleanup scenario progressed successfully.
- The read-only scenario exhausted its retry budget because Worker-produced validation text did not satisfy the deterministic reviewer's strict passing-evidence vocabulary.
- One interrupted-run scenario also demonstrated that a detached supervisor must not inherit the controller's short observation timeout; the harness now gives resumed supervisors a bounded long timeout and waits for `resources.json` deterministically.

These remaining E2E findings are recorded rather than represented as passing.

## Resource state at closeout

- `20260923-luna-xhigh-routing` was not adopted, modified, or cleaned up.
- At delivery time, failed or blocked E2E resources were preserved automatically under the fail-safe policy.
- During explicit user-requested terminal closeout, `wE:tG`, `wE:tH`, `wE:tN`, and `wE:tP` were confirmed `agent_status: done`, then closed individually and verified absent. No workspace was closed.
- The controller tab `wE:t1` remained open only to deliver the final response and can be closed afterward.
- No further real E2E runs should be started until Herdr child-process launch is confirmed not to create a visible PowerShell window under `.herdr/package` or steal desktop focus.

## Remaining follow-up

1. Make Windows Herdr child-process launch provably headless (`windowsHide`/no visible PowerShell chain) before another real E2E run.
2. Normalize or structure Worker validation evidence so the read-only E2E can satisfy deterministic PASS without relying on prose vocabulary.
3. Re-run `npm run e2e:supervisor` and the live TypeSafe smoke test when explicitly enabled with valid environment credentials.
