# Architecture

PARITY runs user-owned staging projects and reproducible bundled examples. Both use real HTTP. A project supplies contracts, endpoint configuration, trusted Python scenarios, and optional business assertions.

`project.py` owns configuration and scenario lifecycle. `comparison.py` validates response schemas and compares JSON without confusing booleans and numbers. Identifier normalization is explicit and bijective. Reports identify unchecked business effects and record contract, scenario, and adapter fingerprints.

`agent.py` runs a bounded plan/source/diagnostic loop. It receives contracts, notes, deltas, and results, but not scenario source or private assertions. `plans.py` checks structural references; Python implements the migration. Atomic run metadata and recorded provider responses support interruption and continuation.

`runtime.py` supplies durable JSON, stable identifiers, a host-local lock, and bounded HTTP. Adapters return an HTTP response or the original `(status, JSON)` pair. The worker records write requests before dispatch and their outcome afterward; workflow-specific checkpoints remain adapter-owned.

The parent owns credentials and API targets. Its relay adds configured authentication headers. Workers receive a loopback endpoint on macOS or a Unix socket on Linux. Linux workers have a separate network namespace; a second parent relay exposes their client-facing HTTP port. Scenario files and the independent checker stay outside worker read roots.

Each example service owns a separate SQL database. SQLite uses `BEGIN IMMEDIATE`; PostgreSQL uses a transaction advisory lock. Adapter checkpoints use SQLite. Host-local locking intentionally serializes adapter requests for bounded checks.

Installed examples live under `parity/examples`. User output belongs in the project's `.parity`. Historical source and plans are immutable replay inputs; demo and validation do not regenerate them.
