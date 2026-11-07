# Release checklist

Primary exercised environment: macOS ARM64, Python3.12, `sandbox-exec`, loopback sockets and writable temporary storage. Linux/bubblewrap is experimental and was not executed for this release. Python3.11/3.13 are not certified by this pass. No-sandbox generated execution must fail closed.

```bash
make setup
make check
make test
make demo
```

For a genuinely fresh local environment:

```bash
make setup VENV=.parity/fresh-release
make test VENV=.parity/fresh-release
make demo VENV=.parity/fresh-release
```

The offline demo uses pinned, previously generated source and spends no model tokens. It demonstrates unchanged client V1 success, direct V2 failure, adapter recovery, committed payment-response loss plus restart/retry, and business-state comparison. Open the report path printed by the command; `artifacts/reports/LATEST` records it. The existing screenshot/report remains historical evidence.

Optional fresh model generation (new calls/quota, not replay):

```bash
codex login
make demo-live
```

The single release stretch attempt is already preserved under `artifacts/generated/moderated-release-001/`; do not regenerate it merely to reproduce archived evaluation. To run its private checker offline:

```bash
.venv/bin/python -c "from pathlib import Path; from parity.moderated_replay import evaluate; print(evaluate(Path('artifacts/generated/moderated-release-001/adapter.py'), Path('.parity/moderated-check')))"
```

Use a new output directory for each replay. No model receives this private checker. `scripts/check_release_evidence.py` checks source/provenance without network access. `scripts/package_release.py` creates the public redacted zip and embedded redaction ledger. Keep raw historical archives private.

Troubleshooting: nullable/missing usage means unobserved counters, not autonomous success. WorkerFailure means infrastructure/resource failure; inspect worker.log, resources.json and termination.json before interpreting a compatibility score. Linux's 2 GiB address-space limit is distinct from the sampled 768 MiB RSS policy; eight AnyIO threads limit stack reservation. Do not disable sandboxing or substitute trusted mode to obtain green results. Missing Seatbelt/bubblewrap capabilities require an explicit unsupported diagnostic. Provider quota failure preserves the run and exits without invented output. Refresh engine fingerprints in `artifacts/pins.json` when engine source changes; retain historical release manifests and generated outputs.

Before distribution: run static checks and full regression, verify original generated artifacts and pins, build/test the archive, inspect `REDACTIONS.json`, and confirm no environment/cache/database/private credential files are present. CI YAML is configuration only until a remote run is actually observed.
