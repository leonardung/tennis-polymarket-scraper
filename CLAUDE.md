# Project Notes

Read this file at the start of each conversation about this repository.

Polymarket ATP tennis capture: a read-only tool that records order books for
live ATP matches into SQLite alongside the live score, plus a dashboard over it.
No trading, no auth, no wallets — do not add them unasked.

The durable project context lives in `agents/`:

- `agents/project-context.md` — what the project is for, the governing
  constraint, workflows, conventions, known pitfalls, and what is out of scope.
- `agents/architecture.md` — module map, data flow, storage schema, the
  invariants a change must not break, and a "where to make a change" table.

Read both before modifying anything. `agents/architecture.md` in particular
covers the score feed's edge-cache behaviour and the `Ratchet`, which is the
easiest part of this codebase to break silently.

`README.md` is the user-facing doc and is thorough — a change that alters
behaviour usually has to update it too. `DNS.md` covers the "cannot reach the
Polymarket API" failure.

## Before saying a change is done

```bash
uv run python tests/test_offline.py      # ~331 checks, no network, plain script
cd frontend && npm run build             # typechecks, then writes into the package
```

The frontend build output under `src/polymarket/dashboard/static/` **is
committed**. A front-end change is not delivered until that has been rebuilt and
committed; for Docker, also `docker compose build`.

There is no CI, no linter config, and no test framework — those two commands are
the whole gate.

Update the relevant file in `agents/` when durable project context,
architecture, workflow, or user preferences change.
