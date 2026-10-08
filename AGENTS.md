# skill-wp Project Rules

## Source And Delivery

- This repository is the development source for the `wp` skill.
- Canonical development and delivery now use the public GitHub repository `https://github.com/zenenznze/skill-wp`; `origin/main` is the authoritative development upstream. Gitea is retained only as a legacy source, not the default delivery target.
- Local pre-migration history and bundle backups are private. Push only the reviewed development branch (`git push origin main`); never use `--all`, `--mirror`, or merge the old Gitea ancestry into public GitHub. Preserve the current implementation during migration.
- Public release tooling, when used, publishes only the Git-tracked set from this checkout. It must not copy untracked local state into a release.

## Public Tracked Set

The public skill may contain only these top-level paths:

- Root files: `.env.example`, `.gitignore`, `LICENSE`, `NOTICE.md`, `README.md`, `SKILL.md`, `AGENTS.md`, `package.json`, `package-lock.json`, `tsconfig.json`.
- Directories: `agents/`, `assets/`, `bin/`, `extensions/`, `references/`, `scripts/`, `src/`, `tests/`, `tests-ts/`.

The following paths are local-only and must remain ignored and untracked:

- `.agent/` and `wp-state/`: task and runtime state;
- `HANDOFF.md`: legacy root handoff;
- `wp-custom/`: user configuration, ideas, and local overrides.

Do not place credentials, raw provider logs, machine-specific configuration, or user custom content in tracked files.

## Build-In-Public Privacy Gate

- Before every commit run `python3 scripts/public_privacy_check.py` against staged Git blobs; before every public push run `python3 scripts/public_privacy_check.py --tree HEAD`, inspect the complete outgoing commit range, and review the diff. Pattern checks do not replace manual review.
- Use project-relative paths, environment variables, `<user>` or `/path/to/...` instead of real usernames, home/checkout paths and private hosts. `.gitignore` excludes files, not sensitive strings inside tracked code/docs.
- Keep credentials, environment files, runtime/task state, dependencies/build output, raw logs and local migration backups ignored and untracked. `.env.example` must contain placeholders only. Do not force-add ignored files.
- Synthetic privacy-scanner fixtures are allowed only after confirming they contain no real values. Never print detected credential values; report only file/category.
- Preserve MIT LICENSE and agent-sop NOTICE. Public availability is not permission to redistribute unlicensed third-party code.
- A later ignore rule or clean commit does not erase historical exposure. Report any historical privacy finding and require explicit authorization before rewriting history.

## Implementation

- `SKILL.md` is the runtime entrypoint and must stay concise; put detailed protocol explanations in `references/`.
- Deterministic behavior belongs in `scripts/` and must have focused tests under `tests/`.
- The current calling Agent owns repository understanding, routing, acceptance, and Git delivery. Client runners own only bounded execution in the target execution root.
- `SKILL.md` and `extensions/wp.ts` are the two formal runtime entries and must share the TypeScript core in `src/core/`.
- Persistent WP state lives below `${WP_STATE_DIR:-~/.agents/state/wp}`, never below the target repository. Checkout-local `wp-state/` is legacy read-only migration input.

## Verification

Run the relevant checks before delivery:

```bash
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/verify_result.py --help
python3 scripts/verify_graph.py --help
npm test
```

Review the complete diff, then commit and push the intended paths to GitHub origin/main. Do not force-add ignored local state.
