# SQLite for tracking store

The tracking server uses DuckDB as its embedded store. DuckDB connections are not thread-safe and do not support concurrent read-only and read-write connections to the same file. This causes the `/query` HTTP endpoint to silently return empty results under concurrent access.

## Decision

Replace DuckDB with SQLite in WAL mode.

- **STRICT tables** enforce type discipline equivalent to DuckDB (`TEXT`, `REAL`, `INTEGER` instead of `VARCHAR`, `DOUBLE`, `BIGINT`; booleans stored as `INTEGER`).
- **Single write connection** guarded by a `threading.Lock`. The gRPC server's `ThreadPoolExecutor` (10 workers) calls `insert_event` concurrently; the lock serializes fast single-row INSERTs.
- **Per-request read-only connections** for HTTP queries. WAL mode allows concurrent readers that don't block the writer and a writer that doesn't block readers.
- **No write queue** — `SendEvent` is synchronous (returns `Ack` after INSERT). A queue would either sacrifice durability (fire-and-forget) or add latency (wait-for-ack) for no throughput gain. The lock on a fast local-file INSERT has no meaningful ceiling.

## Rationale

### Why not fix DuckDB

DuckDB is designed for embedded analytics (OLAP), not concurrent OLTP. Its concurrency limitations are fundamental:

- A single connection cannot be used safely across threads (result objects are tied to connection state).
- Concurrent read-only + read-write connections to the same file are not supported.

No locking strategy works around this. A `threading.Lock` around the connection still produces corrupted result objects. Per-request connections fail when the writer holds a read-write connection.

### Why SQLite over PostgreSQL

The tracking server is a single-user, single-process tool. It owns one database file. PostgreSQL solves multi-user concurrent writes — a problem the project doesn't have. The gRPC server is the sole write gateway; everything flows through `SendEvent`. Adding PostgreSQL would introduce operational dependency (database server, auth, backups) for capability that goes unused. SQLite → PostgreSQL is a straightforward migration if the project ever outgrows single-user.

### Why not stay on DuckDB and serialize everything

Serializing reads and writes through one connection would work for correctness but blocks dashboard queries during write bursts. WAL mode gives read/write concurrency for free.

### Why STRICT tables

DuckDB enforces column types strictly. SQLite's default type affinity would accept a string in a `REAL` column. STRICT tables preserve the same discipline. The schema is simple and stable — no need for SQLite's flexibility features that STRICT disables.

## Consequences

- `duckdb` drops from `jernerics-server` dependencies (stdlib `sqlite3` instead).
- The dashboard dev server (separate repo) migrates independently — same change, different schedule.
- Default database file extension changes from `.duckdb` to `.sqlite`.
- If the project later needs concurrent writers (multiple users, separate processes), SQLite → PostgreSQL is the migration path. The schema ports almost verbatim.
