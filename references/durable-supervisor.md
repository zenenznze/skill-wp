# Durable Herdr Supervisor

WP's TypeScript control plane is a crash-safe semantic supervisor, not a dispatch wrapper. The shared implementation lives in `src/core/supervisor.ts`.

`wp_task_run` and `wp_task_resume` never execute the wait loop inside the controller turn. They launch a detached Node supervisor, persist its PID, and return a running receipt immediately. The detached process owns waiting, review, and bounded retries, so the controller remains available for user messages and can be reloaded without stopping an active task. Repeated run/resume calls detect the live supervisor and return `already_running`; progress is read through short `wp_task_status` calls.

## Attempt lifecycle

For each bounded attempt WP:

1. acquires the task's single-writer `supervisor.lock`;
2. probes `resources.json` and reconnects to the recorded Herdr tab/agent when reachable;
3. otherwise creates one visible background tab, starts Pi, persists the resource IDs, and sends the bounded HANDOFF prompt;
4. waits for `result.json` while archiving terminal output;
5. compares the real Git working tree with `write_scope` and `result.changed_files`;
6. archives prompt, resources, output, result, and review below `attempts/attempt-NN/`;
7. returns `PASS`, preserves an external `BLOCKED`, or starts a bounded retry with review findings.

Herdr `idle`, `done`, process exit, and terminal prose are lifecycle evidence only. They never create semantic success. A timeout preserves the resource and task state so a later `resume` can reconnect rather than create a duplicate tab.

## Durable state

```text
~/.agents/state/wp/repos/<repo-id>/tasks/<task-id>/
  HANDOFF.md
  task.json
  resources.json
  result.json
  events.jsonl
  supervisor.lock
  supervisor-process.json
  supervisor.log
  attempts/attempt-NN/{prompt.txt,resources.json,output.json,result.json,review.json}
  reviews/attempt-NN.json
  checkpoints/
```

All JSON state writes use same-directory temporary files, fsync, and atomic rename. Events are appended and fsynced. A stale PID lock is recoverable; a live owner is rejected.

## Review and acceptance

The deterministic reviewer parses the result contract and obtains the actual repository changes from `git status --porcelain=v1 -z`. It rejects:

- changed files outside `write_scope`;
- actual changes omitted from `result.changed_files`;
- claimed files absent from Git changes;
- out-of-scope claims;
- invalid, missing, failed, or internally non-passing results.

`accept` requires the current attempt's persisted PASS review and task status `review`. Only then does WP rename the real tab to `完成-<task-id>` and persist `accepted`.

## Real end-to-end validation

Run only the sandbox scenario when validating this supervisor:

```bash
npm run e2e:supervisor
```

The scenario compiles as setup inside the E2E, initializes a temporary Git repository, launches a real headless Pi controller with the WP extension, creates a real Herdr worker, forces a deterministic first-attempt RETRY, reaches PASS on a later attempt, and inspects persisted archives and the live Herdr tab. It does not push or deploy.
