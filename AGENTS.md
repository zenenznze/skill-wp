# skill-wp Project Rules

## Source And Delivery

- This repository is the development source for the `wp` skill.
- Repository changes are committed and pushed to the configured Gitea upstream as part of delivery.
- Public release tooling, when used, publishes only the Git-tracked set from this checkout. It must not copy untracked local state into a release.

## Public Tracked Set

The public skill may contain only these top-level paths:

- Root files: `.env.example`, `.gitignore`, `NOTICE.md`, `README.md`, `SKILL.md`, `AGENTS.md`.
- Directories: `agents/`, `assets/`, `references/`, `scripts/`, `tests/`.

The following paths are local-only and must remain ignored and untracked:

- `.agent/` and `wp-state/`: task and runtime state;
- `HANDOFF.md`: legacy root handoff;
- `wp-custom/`: user configuration, ideas, and local overrides.

Do not place credentials, raw provider logs, machine-specific configuration, or user custom content in tracked files.

## Implementation

- `SKILL.md` is the runtime entrypoint and must stay concise; put detailed protocol explanations in `references/`.
- Deterministic behavior belongs in `scripts/` and must have focused tests under `tests/`.
- The current calling Agent owns repository understanding, routing, acceptance, and Git delivery. Client runners own only bounded execution in the target execution root.
- Preserve the skill-local state boundary: wp-owned persistent files go below `wp-state/` in the skill checkout, never below the target repository.

## Verification

Run the relevant checks before delivery:

```bash
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/verify_result.py --help
```

Review the complete diff, then commit and push the intended paths to Gitea. Do not force-add ignored local state.
