# Atomic Work Contract

The Atomic Work Contract is the deterministic readiness boundary for protocol
version 2 task packages. It does not decide whether a goal is semantically too
large. The controller owns that judgment and may split a goal whenever the
work cannot be independently accepted, has multiple unrelated outcomes, or
would need different write owners or restart points.

## Required Shape

Every new HANDOFF has `task_protocol_version: 2` in frontmatter and one fenced
JSON section headed `# Atomic Work Contract`:

```json
{
  "single_outcome": "One observable result.",
  "deliverables": ["Owned file or artifact"],
  "write_scope": ["src/example/**"],
  "read_only": false,
  "acceptance": ["An independent check passes"],
  "resume_boundary": "Resume at the focused parser test after a failure."
}
```

`single_outcome`, each deliverable and acceptance item, and
`resume_boundary` must be non-empty and free of placeholder text. Every
`write_scope` entry is a repository-relative path or glob. A writing task must
declare a non-empty scope; a read-only task must declare `read_only: true` and
an explicit empty `write_scope: []`.

The parser is bounded and local: it reads only the HANDOFF, caps the file and
contract sizes, parses JSON with the standard library, and reports all
structural errors. No YAML parser or network access is needed.

## Readiness And Compatibility

`run_task.py` validates the package before capability derivation, executor
routing, settings changes, or subprocess launch. `run_graph.py` validates
every task node before either dry-run output or launch. For a v2 graph node,
`writes` is required and must equal the contract `write_scope` after ordering;
this makes ownership visible to both the executor and conservative scheduler.
The package also needs a populated `# Acceptance Criteria` checklist and a
non-empty fenced `# Validation Commands` block; placeholders outside the JSON
contract are rejected as incomplete package state.

An unversioned HANDOFF is classified as `legacy` but is rejected by default.
`run_task.py --allow-legacy-task-package` explicitly opts into the bounded
compatibility path; `run_graph.py` accepts the same flag, rejects every legacy
node without it, and forwards it to every legacy child invocation. The old
graph write behavior remains available until a controller migrates the task.
A malformed version marker is not legacy and fails preflight. Legacy
classification is reported in execution diagnostics and is never treated as
satisfying the v2 contract.

## Work Hierarchy

The durable execution hierarchy is:

```text
Goal
  -> Graph
    -> Atomic Task Package
      -> Attempt
        -> Turn / Tool
      -> Validation
    -> Controller Acceptance
```

- **Goal**: the user-visible outcome and global boundary.
- **Graph**: optional dependency and write-conflict plan for multiple tasks.
- **Atomic Task Package**: one outcome, owned deliverables, write scope,
  independent acceptance, and a restart boundary.
- **Attempt**: one bounded executor invocation with its own terminal artifacts.
- **Turn / Tool**: implementation activity inside an attempt, never an
  independently schedulable graph node.
- **Validation**: executable evidence recorded by the attempt and rerun by the
  controller.
- **Controller Acceptance**: independent diff and acceptance review that
  decides whether the goal is complete.

## Split Or Do Not Split

Keep work in one package when it has one observable result, one coherent owner,
one compatible write scope, one independent acceptance boundary, and one clear
restart location. Split it into separate packages when any of these conditions
would otherwise be ambiguous:

- the work has multiple independently useful outcomes;
- deliverables need different owners or overlapping write scopes;
- acceptance requires unrelated checks or different reviewers;
- a failure would need materially different restart locations;
- graph dependencies make part of the work ready before the rest.

Do not split merely because there are several files, functions, tool calls, or
implementation steps. Do not use the deterministic validator as a subjective
complexity or task-size score; it checks contract completeness and ownership,
while the controller decides semantic decomposition.
