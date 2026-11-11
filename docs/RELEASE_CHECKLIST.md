# Release checks

```sh
make setup
make check
make test
make demo
.venv/bin/python -m build
.venv/bin/python scripts/check_release_evidence.py
```

Tests cover an installed wheel outside the checkout, external-project requests, response schemas, credential isolation, recovery, interrupted generation, bundled replay, and OS isolation. CI runs on macOS and Linux; Linux needs bubblewrap and unprivileged namespace support.

Refresh current engine fingerprints in `artifacts/pins.json` when intentionally changing pinned source. Preserve example source/plan bytes and recorded model responses. Full historical transcripts remain in Git history; private archives and new runs belong under ignored `.parity`.

`scripts/package_release.py` creates a public ZIP and redaction ledger. Inspect it before distribution: credentials, databases, caches, environments, and private workspaces must be excluded. Redacted transcripts intentionally have different hashes; source and plans retain their original bytes.

Optional PostgreSQL workflow/restart checks use a disposable local cluster:

```sh
.venv/bin/python scripts/check_postgres.py
```

Docker Compose supplies the example commerce services with separate databases. It does not deploy a production adapter gateway.
