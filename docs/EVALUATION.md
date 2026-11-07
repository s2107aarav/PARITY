# Evaluation

Recorded results below describe historical runs. Additional concurrency, resource-limit and approval-receipt fixture evidence is retained in `artifacts/releases/hardening/`; the approval fixture passed 4/4 private cases after one seven-call generation attempt. Run the commands below to validate the current source. Linux policy changes do not establish Linux execution support. The rules mapper is a weak baseline and the rejected single-shot output remains unscored.

All fixture suites use seed 0. Replay applies the same logical legacy actions to freshly seeded, equivalent but isolated V1 and adapter→V2 states. The checker compares responses and independently reads business effects. This is a small engineering evaluation, not a statistical benchmark.

## Reproduce

```bash
make setup
make test
make check
make demo
.venv/bin/parity validate --adapter commerce-live-002 --suite commerce
.venv/bin/parity validate --adapter transfer-live-001 --suite transfer
.venv/bin/python scripts/check_postgres.py
```

Checked-in evidence is in `artifacts/reports/clean-demo/`: the HTML/JSON report, each candidate's `evaluation.json`, per-scenario before/after `result.json`, and actual worker HTTP traces. New demos use fresh timestamped directories recorded in `artifacts/reports/LATEST`. No LLM tokens are spent by replay.

## Observed outcomes

| Candidate/control | Outcome | Generation information and validation |
|---|---|---|
| PARITY commerce revision 2 | 15/15 workflows | Specs, notes, typed tools, public diagnostics and actual model-authored repair |
| Manual commerce reference | 15/15 workflows | Human-authored calibration; never supplied to model |
| Rules-only operationId mapper | 0/15 workflows | Same specs; static endpoint/path matching, no semantic workflow synthesis |
| Single-shot LLM | Plan rejected; workflows **not run** | One authenticated call; same initial specs/notes/ABI, no tools or feedback |
| Generated ticketing transfer | 6/6 workflows | Same generic agent/plan/runtime; second-domain notes and public diagnostics |
| Defective calibration adapters | 5/5 rejected | Same independently owned commerce predicates |

The original test run recorded 55 passing tests across unit, integration, end-to-end and adversarial cases; later runs add coverage. Hypothesis exercises 12 deterministic state-machine examples of up to 25 checkout/retry/cancel actions. An upstream Starlette/httpx deprecation warning remains visible.

The 15 commerce workflows cover ordinary checkout/retrieve/list/stock; business errors; discarded client response; committed payment response loss plus adapter restart; reservation failure; confirmation response loss; repeat cancellation; five-order pagination with failed drafts; concurrent duplicate checkout; changed-body key conflict; compensation failure; compensation response loss/restart; cancellation response loss/restart; corrected rejected key reuse; and compound-invalid error precedence.

The six transfer workflows cover ordinary create/retrieve, retry, publication response loss/restart, changed-body key conflict, invalid/missing input, and concurrent duplicate creation. Additional tests cover duplicate SKU aggregation, persisted IDs, other step fault boundaries, SQL rollback, direct V2 reconciliation/refund, parser/cycle/capability checks, malformed model output and real OS file denial.

## Real failures and repair

`commerce-live-001` produced executable orchestration and passed the original public smoke suite. A later public diagnostic found that stock plus amount being invalid returned V2's `422 AMOUNT_MISMATCH`, while V1 requires `409 OUT_OF_STOCK`. The failing result is retained in `commerce-live-002/public-v1/public-result.json`.

Six subsequent authenticated model calls inspected evidence, revised the plan/source, ran public checks and finished. Both source versions, both plans, raw responses, parent hashes and the passing `public-v2` result are preserved. The repair preflights a new workflow and persists validation completion so ambiguous reservation retries do not incorrectly recheck already-debited stock. Independent evaluation then accepted revision 2. No human modified the generated source.

The original commerce run hit a real provider quota limit after its public checks. Its eight failed requests are preserved. The engine now stops immediately on provider unavailability. Its original missing final tool action is not retroactively labeled successful.

The real single-shot baseline returned a plan containing non-HTTP workflow operations that the strict IR rejected. Raw response, candidate plan, rejected source and failure manifest are saved. It received no corrective second call. **No workflow compatibility percentage is assigned to this unexecuted candidate.** The full agent's plan errors, in contrast, received validator feedback and were revised. The final repair also received an additional public diagnostic; these trials do not have identical total information budgets.

## Independent checker calibration

The fake-success mutant returns plausible checkout, retrieval, listing and stock JSON but creates no orders or payments. Response matching alone could accept it; business-state comparison rejects it. Other mutants alter money, create duplicate order/payment effects, leak stock in an unsettled reservation, or turn a decline into generic 500. Exact mismatches and state snapshots remain in `calibration/`.

Checker files are outside generated-worker read roots. Evaluator/client/source hashes are recorded and pinned. The agent receives only public diagnostics. See the security guide for the trusted-owner limitation.

## Observed time and usage

Each paired HTTP action records `v1_ms` and `adapter_ms`. The report computes median **paired added latency** from `adapter_ms - v1_ms`. It does not subtract unrelated averages. Scenario elapsed time additionally includes fixture/worker startup and shutdown. These are incidental local measurements, affected by imports, I/O and concurrent checks; they are not load-test throughput claims. Exact values are in report JSON.

| Run | Actual model-call attempts | Recorded elapsed time |
|---|---:|---:|
| Initial commerce | 14: 6 successful responses, 8 quota failures | 183.720 s |
| Commerce repair | 6 successful responses | 123.571 s |
| Transfer | 7 successful responses | 102.442 s |
| Single-shot | 1 successful response | 106.413 s |

Every call retains usage/token counters when available, elapsed time and CLI version. Codex CLI was 0.153.4. Its precise default server model identity and monetary cost were not disclosed and remain null; subscription token counts are not an inferred API bill. Future runs may specify `--model`; historical identity cannot be invented.

## PostgreSQL and reproducibility

A disposable local PostgreSQL 18.3 cluster was actually started. Five sequential commerce workflows passed with distinct V1/V2 PostgreSQL databases: ordinary, payment loss/restart, concurrent retry, cancellation and pagination. PostgreSQL was stopped/restarted and final effects remained identical. Adapter checkpoints remained SQLite with single-host locking. `artifacts/reports/postgres-verification.json` records this separately.

Docker Compose was not run because the daemon and Compose CLI plugin were unavailable. macOS isolation was exercised; Linux bubblewrap and Windows were not. Fresh dependency setup was performed in `.parity/clean-env`, followed by offline demo replay. The lockfile contains actual installed versions.

The final suites passed in tests and repeated canonical demo runs without an observed flaky case. These are limited repeatability controls, not a proof of deterministic concurrency. Failed artifacts were retained. No schema field, amount, status or stock value was broadly normalized away. No statistical superiority or universal migration claim follows from this sample.
