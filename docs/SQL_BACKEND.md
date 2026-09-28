# SQL backend: SQLite or Postgres

The small operational stores (connector ledgers, connector jobs, OAuth
connections, the graph registry, the GitHub and Notion connector stores,
runtime settings) all open their database through
`storage/sql_backend.py::connect(path)`. The same code runs on SQLite (the
default) or on Postgres.

## Switching

| Variable | Value |
| --- | --- |
| `NEURON_SQL_BACKEND` | `sqlite` (default) or `postgres` |
| `DATABASE_URL` (or `POSTGRES_URL`) | e.g. `postgresql://neuron:neuron@localhost:5432/neuron` -- required for `postgres` |
| `NEURON_SQL_POOL_SIZE` | idle connections kept per schema (default 8) |

Nothing else changes: every store is still constructed with its SQLite file
path, and that path decides which Postgres schema it uses.

## Schema-per-file mapping

Each SQLite file maps to its own Postgres schema named after the file stem
(`storage.sql_backend.schema_for_path`), lowercased with non `[a-z0-9_]`
characters replaced by `_`:

| SQLite file (in `DATA_DIR`) | Postgres schema |
| --- | --- |
| `connector_ledger.sqlite3` | `connector_ledger` |
| `connector_ledger__<graph>.sqlite3` | `connector_ledger__<graph>` |
| `connector_jobs.sqlite3` | `connector_jobs` |
| `oauth_connectors.sqlite3` | `oauth_connectors` |
| `graphs.sqlite3` | `graphs` |
| `github_connector.sqlite3` | `github_connector` |
| `notion_connector.sqlite3` | `notion_connector` |
| `runtime_settings.sqlite3` | `runtime_settings` |

Table names inside a schema are the same as in the SQLite file. The `public`
schema belongs to `storage/postgres.py` (graph-scoped tables) and is never
used by these stores or by the migration script.

## Migrating existing data

`scripts/migrate_sqlite_to_postgres.py` copies every `*.sqlite3` in
`DATA_DIR` (same resolution as the backend: `DATA_DIR` env, else the repo
root) into its schema.

1. **Dry run** (default; SQLite opened read-only, Postgres only queried):

   ```bash
   DATABASE_URL=postgresql://... uv run python -m scripts.migrate_sqlite_to_postgres
   ```

   Prints, per file, the target schema and per table the SQLite row count and
   the Postgres row count (`absent` if the table does not exist yet).
   Options: `--data-dir DIR`, `--database-url URL`, `--only STEM` (repeatable,
   e.g. `--only graphs --only connector_ledger`).

2. **Stop the app** (API and job worker) so SQLite does not change mid-copy.

3. **Apply**:

   ```bash
   DATABASE_URL=postgresql://... uv run python -m scripts.migrate_sqlite_to_postgres --apply
   ```

   Target tables are created by the owning store class running against
   Postgres, so DDL and column migrations match the app; tables of unknown
   files (or of a store that is not Postgres-aware yet) are mirrored from
   `sqlite_master`. Each file is copied in one transaction: rows (column
   intersection, in batches), `BIGSERIAL` sequences reset to `MAX(id)`, then
   row counts verified -- any failure rolls that file back. A file whose
   Postgres tables already contain rows is refused; `--replace-existing`
   truncates only that file's tables in its own schema and copies again.
   Exit code is non-zero if any file was refused or failed.

4. **Switch**: set `NEURON_SQL_BACKEND=postgres` and `DATABASE_URL` in `.env`.

5. **Restart** the API and job worker.

## Rollback

Unset `NEURON_SQL_BACKEND` (or set it to `sqlite`) and restart. The migration
never modifies or deletes the SQLite files, so the app picks up exactly where
it was before the migration. Anything written while running on Postgres stays
in Postgres only.

## Tests on Postgres

`tests/conftest.py` pins the suite to SQLite unless told otherwise. To run
every store test on Postgres (each test gets a unique schema prefix via
`NEURON_SQL_SCHEMA_PREFIX`, dropped afterwards):

```bash
NEURON_TEST_DATABASE_URL=postgresql://neuron:neuron@localhost:55432/neuron \
NEURON_TEST_SQL_BACKEND=postgres uv run pytest
```

With only `NEURON_TEST_DATABASE_URL` set, tests parametrized with the
`sql_backend` fixture run both legs and the migration-script test
(`tests/test_migrate_sqlite_to_postgres.py`) runs; without it they skip.

## Writing portable store SQL

The shim translates `?` placeholders, DDL types (`INTEGER PRIMARY KEY
AUTOINCREMENT` -> `BIGSERIAL PRIMARY KEY`, `REAL`, `BLOB`), `PRAGMA
table_info`, and no-ops `BEGIN` and bare `PRAGMA name` statements. (At the
time of writing `PRAGMA name=value`, e.g. `PRAGMA journal_mode=WAL`, is *not*
recognised and fails on Postgres -- guard it with `sql_backend.is_postgres`.) Everything else must be SQL
both engines accept: `ON CONFLICT ... DO NOTHING / DO UPDATE SET c =
excluded.c` instead of `INSERT OR IGNORE/REPLACE`, `COALESCE` instead of
`IFNULL`, `RETURNING id` instead of `lastrowid`, `LOWER(x) LIKE LOWER(?)` where
SQLite's case-insensitive `LIKE` matters, and `sql_backend.IntegrityError` /
`OperationalError` tuples in `except` clauses.
