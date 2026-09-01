# Pokerit Agent Instructions

## Task mode and mutation authorization

- Requests to inspect, explain, review, compare, diagnose, identify gaps, verify
  behavior, or answer "why" are read-only. They do not authorize source or test
  edits, dependency changes, migrations, service lifecycle actions, or runtime
  configuration changes.
- Finding a bug, inconsistency, or improvement opportunity does not authorize
  fixing it. Report the finding and wait for explicit implementation approval.
- Modify files only when the user explicitly asks to implement, fix, change,
  create, or update something. Keep mutations within that requested scope.
- If a request could reasonably mean either diagnosis or implementation, ask
  before the first mutation.
- A request to test or verify authorizes appropriate non-disruptive checks; it
  does not authorize changing code to make a failing check pass.

## Live application safety

- Treat the local application as a potentially active user session. If the user
  says or implies that they are currently playing, perform read-only inspection
  only unless they explicitly authorize a live-affecting action.
- The app and worker mount source files and watch them for changes. Editing source
  can hot-reload live behavior even without an explicit restart.
- During an active session, do not edit source or static assets, restart/rebuild/
  stop containers, kill processes, run migrations, alter the active database, or
  run tests that could touch application state.
- `docker compose ps` is a safe status check. Never run
  `docker compose down -v` unless the user explicitly requests deletion of the
  persisted PostgreSQL and Ollama volumes.
- The current live hand is held in memory and may be lost after an app restart or
  broken WebSocket connection. Completed hands are persisted incrementally.

## Working tree and environment

- Run `git status --short` before editing. Treat existing modifications and
  untracked files as user-owned unless clearly established otherwise.
- Do not overwrite, revert, discard, stage, commit, or otherwise alter unrelated
  user changes. Do not use destructive Git commands.
- Do not create commits, branches, tags, releases, or pull requests unless asked.
- This is a Python 3.11 project managed by `uv`; `uv.lock` is authoritative for
  locked dependencies and pytest is provided by the `dev` extra.
- Before claiming that Python, `uv`, pytest, or another dependency is missing,
  inspect `pyproject.toml`, `uv.lock`, and the repository-local `.venv`, then try
  the project-native command and report its exact error.
- Do not infer project capability from the global Python environment. Do not
  install packages globally or change dependencies unless the task requires it.

Canonical commands:

```text
uv sync --extra dev
uv run --extra dev pytest -q
uv run --extra dev pytest -q <test paths or -k expression>
```

## Product contracts

- Keep these coaching surfaces distinct:
  - `src/ai_functions/coach_engine/`: interactive live/saved-hand/general coach.
  - `src/ai_functions/game_review/`: structured single-game after-game review.
  - `src/ai_functions/history_evaluation/`: scope-isolated rolling-history report.
- Do not transfer prompts, terminology, evidence behavior, or assumptions between
  these surfaces without checking the relevant contract and tests.
- Internal evidence keys and user-facing evidence labels are different layers.
  The canonical after-game display mapping is `EVIDENCE_TYPE_LABELS` in
  `src/poker_trainer/static/js/app.js`.
- Internal evidence sources are constructed primarily in
  `decision_snapshot.py`, `game_review/merge.py`, and
  `preflop_ranges/knowledge.py`; interactive-coach wording lives in
  `coach_engine/engine.py`.
- For a terminology investigation, identify every affected surface, distinguish
  internal provenance from displayed copy, and report the gap. Do not rename,
  normalize, or unify terms until the user authorizes the exact change.
- When an authorized terminology change affects a shared product concept, update
  the relevant prompt, UI mapping, tests, and user-facing documentation together.

## Poker and evidence invariants

- Never expose or persist an opponent's hidden hole cards unless they were
  actually revealed at showdown. A fold win is not a showdown.
- Decision Snapshots must exclude everything occurring after the reviewed hero
  decision, including later actions, board cards, showdown cards, and results.
- Keep code-owned deterministic values deterministic: pot/stack arithmetic,
  legal actions, statistics, thresholds, sample readiness, tags, severities, and
  citations must not be overwritten or invented by LLM narration.
- Keep scenario, table-size, stack-depth, and cash/MTT profile scopes isolated.
  MTT presets are fixed-level chip-EV training scenarios, not full tournaments;
  do not assume payouts, blind progression, bubble pressure, or ICM.
- Preflop range knowledge covers supported RFI nodes only and must fail closed
  outside an exact matching pack. Never invent exact mixed frequencies.

### Solver evidence boundary

- Do not describe advice as solver-backed merely because it resembles GTO play.
  `AI GTO` is a configured bot style, not a solver query.
- Use the internal `solver_node` / user-facing `Solver result` category only when
  a real solver integration successfully queried a matching node.
- A solver-backed result must retain enough provenance to identify the solver,
  version/configuration, queried node, and relevant game assumptions.
- If no matching solver node was queried, classify the conclusion as
  `AI strategy judgment`, even when confidence is high.
- Do not add a solver integration or change the evidence taxonomy without an
  explicitly authorized implementation task.

## Database, tests, and privacy

- Alembic is the canonical schema upgrade path. `scripts/init_db.py` is only for
  a fresh throwaway database. Do not create, apply, downgrade, or edit migrations
  without explicit authorization for a schema or data change.
- Database tests must use `TEST_DATABASE_URL`, never the application's
  `DATABASE_URL`. The default dedicated test database is `poker_test`; if its
  isolation cannot be verified, do not run database tests without asking.
- Run targeted tests first. Run the full suite for cross-cutting changes,
  migrations, shared contracts, or release work. A skipped test is not a pass.
- Never commit or reveal `.env`, credentials, cookies, database dumps, or tokens.
  Do not print secret-bearing environment variables.
- Treat `logs/prompts.jsonl` as sensitive user/model data. Do not publish or
  include unsanitized prompt logs in patches or upstream contributions.

## Completion and sources of truth

- Report the exact checks run and distinguish passed, failed, skipped, and not
  run. Do not call an untested change verified.
- Use `README.md` for the current architecture, setup, and operational overview;
  use code and tests for executable behavior. Treat `docs/UPSTREAM_CHANGES.md` as
  a reviewer snapshot and cross-check it against current code and Git history.
- Update README or CHANGELOG only when the authorized change affects users,
  setup, behavior, or release documentation.
