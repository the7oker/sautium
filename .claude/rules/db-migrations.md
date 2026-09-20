---
paths:
  - "desktop/migrations/**"
  - "backend/db_migrate.py"
  - "desktop/db_init.py"
  - "backend/models.py"
---

# Migration & DB workflow

The invariant (001 + idempotent numbered deltas, one runner, never ALTER a
node by hand) and the query-time gotchas stay in the root `CLAUDE.md`;
this file carries the mechanics.

- **Schema = `desktop/migrations/001_initial.sql` + numbered deltas.** 001
  is the fresh-install baseline and the single readable source of truth —
  fold every schema change into it. Since 2026-08-25 there are external
  nodes to support (the first appeared that night, schema from 001, no
  data yet), so every schema change ALSO ships as `NNN_<change>.sql`, an
  IDEMPOTENT delta (`IF NOT EXISTS`, `DROP CONSTRAINT IF EXISTS` + `ADD`,
  `DO $$ … EXCEPTION WHEN duplicate_object`) for databases that already ran
  the earlier files — the same DDL as the 001 block it mirrors
  (`002_invite_tokens.sql`, `003_gear_fk_cascade.sql` are the pattern). A
  fresh node runs 001 and then every delta, so a delta must be a no-op on
  the schema 001 just created. Never ALTER a node by hand.
- **One runner, every node.** `desktop/db_init.apply_migrations` applies
  pending files in name order and records them in `_schema_migrations`.
  The launcher calls it at service start and after an update
  (`updater.has_new_migrations` = an added `migrations/*.sql` in the
  pulled range); the backend calls `backend/db_migrate.apply_pending()` in
  its lifespan before anything serves — Docker included, so the master no
  longer needs hand-applied DDL (it adopted 001 as its baseline on
  2026-08-25; `adopt_baseline` does that for any pre-runner database). A
  node only receives a delta once it is committed and pushed.
- **Data migrations are Python steps in `backend/db_migrate.py`**, keyed
  by marker rows in the same table (`identity_rule_v{N}`): the identity
  rule (`uuid_utils.IDENTITY_RULE`) is re-normalized at startup when the
  recorded rule is older than the code's. Bump the constant with every
  change to `normalize`/`normalize_key`; never change the rule without it.
- **Trying DDL out** happens on the rehearsal database, not the live one:
  restore the latest `data/backup/*.dump` into `music_ai_test`
  (`pg_restore -L` without the `mb_*` / `lb_*` data), run the delta there, then
  commit it as `NNN_*.sql` and let the runner apply it (restart the
  backend).
- **PostgreSQL ENUM type changes** require this exact sequence (a straight
  `ALTER TYPE` fails):
  ```
  ALTER TABLE t ALTER COLUMN c DROP DEFAULT;
  ALTER TABLE t DROP CONSTRAINT IF EXISTS chk_c;
  ALTER TABLE t ALTER COLUMN c TYPE new_enum USING c::new_enum;
  ALTER TABLE t ALTER COLUMN c SET DEFAULT 'x'::new_enum;
  ```
- **SQLAlchemy ENUM** uses `postgresql.ENUM(..., create_type=False)` — the
  SQL migration owns type creation so `Base.metadata.create_all()` stays
  lightweight and tests don't try to recreate existing types.
