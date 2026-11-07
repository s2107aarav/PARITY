# Demo script

Release instructions and prerequisites are in `RELEASE_CHECKLIST.md`. Use the report printed by a new offline `make demo` for current resource-policy results; screenshots and earlier reports are historical. The curated approval challenge is separately labeled release evidence (4/4 private checks, one real generation attempt), not a live step in the credential-free flagship demo.

Prepare dependencies with `make setup`. Offline replay uses pinned **real generated artifacts**, clearly labeled. It spends no LLM tokens.

## Approximately 90 seconds

Run `make demo`. Startup time varies; use the saved `artifacts/reports/clean-demo/index.html` for a timed walkthrough or recording.

1. The three-path strip shows unchanged checkout returning 201 on V1, 404 on direct V2, and recovery through the generated adapter.
2. Select **ordinary**: explain draft → reserve → capture → confirm. Inspect actual business effects and saved `worker/state/http.jsonl` step traces.
3. Select **payment_loss_restart**: capture committed, reply lost, PENDING returned, worker restarted, same-key retry finished with one charge.
4. Open public diagnostic trials and source revision diff: original stock/amount precedence failure, real model-authored repair, passing public result. Return to separate independent replay scores.
5. Show fake-success rejection, single-shot generation rejection, and explicit limitations. Do not present public diagnostics as hidden-test success or replay as live generation.

## Five-minute engineering walkthrough

```bash
.venv/bin/parity inspect --old contracts/commerce/v1.json --new contracts/commerce/v2.json
.venv/bin/parity validate --adapter commerce-live-002 --suite commerce
.venv/bin/parity validate --adapter transfer-live-001 --suite transfer
```

Inspect the generated revision-2 plan/source, parent link, `state.json`, original failed public trial, and raw `calls/*` evidence. Show a scenario's before/after state and exact charge/refund/stock predicates. The fake-success result under `clean-demo/calibration/` demonstrates why JSON is insufficient.

Show the separate ticketing plan/source using the same engine. Optionally run `.venv/bin/python scripts/check_postgres.py` to create a disposable PostgreSQL cluster, exercise business paths and verify restart persistence.

## Actual live generation

```bash
codex login
make demo-live
```

This invokes an authenticated model, prints typed tool choices and writes new artifacts. It can take minutes and use account quota. Validate the printed artifact directory afterward. Failure exits nonzero and preserves the trajectory; live output is not guaranteed to match the historical artifact.

`generate --resume-artifact <own-prior-artifact> --out <new-directory>` continues with the model's own earlier source and public diagnostics. The shipped repair is already recorded. Replaying it is not a new generation event.
