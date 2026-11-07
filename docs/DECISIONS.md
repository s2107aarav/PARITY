# Decisions

1. Python/FastAPI with a synchronous plugin ABI keeps semantic orchestration inspectable. Generated Python owns the migration; engine code owns transport, storage, budgets and validation.
2. Each service owns an isolated SQL database. SQLAlchemy supports SQLite locally and PostgreSQL in Compose. A serialized transactional document table is deliberately small and auditable; this is not a high throughput database design.
3. Docker daemon was unavailable during initial inspection. SQLite plus OS-restricted subprocesses are the local execution path. PostgreSQL/Compose must be labeled unverified unless actually exercised.
4. Checkout commits across multiple V2 transactions. Durable adapter checkpoints, upstream idempotency and reconciliation provide retry recovery, not a distributed transaction.
5. Cancellation/refund/release is one idempotent V2 transaction. Failed drafts remain internal audit records and are not V1-visible orders.
6. Live provider uses authenticated Codex CLI structured output when available; API credentials are not inferred from a subscription. Model artifacts are separate from the manually authored calibration fixture.

7. The single-shot baseline's invalid plan was retained as a generation-stage rejection, rather than repaired into a nominal single-shot result or assigned an invented workflow score. The full agent received real validator feedback.
8. An observed stock-versus-amount error mismatch required a read-only preflight before a new saga. The agent authored the change; its first artifact, failing public trial, revised plan/source and passing trial remain intact. Repeating preflight after ambiguous reservation would break recovery, so validation is checkpointed.
9. A local PostgreSQL 18.3 installation became available for an isolated disposable-cluster check, despite Docker being unavailable. Selected business workflows and database restart were verified; adapter checkpoints remain SQLite/single-host.
10. Provider integration follows [official non-interactive CLI documentation](https://learn.chatgpt.com/docs/non-interactive-mode): structured JSON output, saved events and isolated working directory. The historical default model identity was not disclosed and is left unknown. No authentication material is copied into generated workers.
