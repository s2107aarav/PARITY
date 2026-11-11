# PARITY

Check an API upgrade against the workflows your clients use. PARITY compares two staging APIs, validates HTTP contracts, and runs your business assertions. It can generate a reviewable Python compatibility adapter and check it against the same scenarios.

PARITY is a local development tool. Use disposable staging data. Adapter execution is bounded, single-host, and isolated; it is not a continuously running production gateway.

## Install

Python 3.12 is the tested version. macOS uses `sandbox-exec`. Linux requires `bubblewrap` and permission to create network namespaces. Inspection and direct API comparison do not require a sandbox; executing generated adapters does.

```sh
python3 -m venv .venv
.venv/bin/pip install .
source .venv/bin/activate
parity --help
```

## Try a migration

Create a ticketing project with two different API contracts and one scenario:

```sh
parity init ticket-migration --example tickets
```

Start the example APIs in separate terminals from the same directory:

```sh
parity serve --suite transfer --version old --db .parity/tickets-old.db --port 8001
parity serve --suite transfer --version new --db .parity/tickets-new.db --port 8002
```

This example explicitly enables writes to these local services. Inspect and compare them:

```sh
parity doctor --project ticket-migration/parity.json
parity inspect --project ticket-migration/parity.json
parity validate --project ticket-migration/parity.json
```

Validation fails because the new API replaces `/tickets` with a multi-step issue workflow. Check the bundled, previously generated adapter:

```sh
parity validate --project ticket-migration/parity.json --adapter transfer-live-001
```

The output includes HTML and JSON reports under the project's `.parity/` directory. They identify failed requests and show which business checks ran. This example checks responses; it does not claim to verify business effects you have not declared.

The full offline demonstration additionally checks checkout effects, compensation, concurrency, recovery, and deliberately incorrect adapters:

```sh
parity demo
```

## Use your APIs

Run `parity init my-migration`. Add `old.json` and `new.json` OpenAPI 3.x JSON documents, edit staging URLs in `parity.json`, and write `test_` functions in `scenarios.py`. Writes are disabled by default; enable `allow_writes` only for disposable data you control.

```python
def test_list(case):
    case.request("GET", "/items")

def check_effects(case):
    old = case.old.get("/inventory").json()
    new = case.upstream.get("/inventory").json()
    assert old["available"] == new["available"]
```

Run `parity doctor --project my-migration/parity.json`, then `parity validate --project my-migration/parity.json`. Business effects are marked `not_checked` until you supply `check_effects`.

See [projects](docs/PROJECTS.md) for credentials, setup/teardown, identifier mappings, generation, and recovery.

## Generate an adapter

Install and authenticate the Codex CLI separately, or use the Responses API provider with `OPENAI_API_KEY` and `--model`.

```sh
parity generate --project my-migration/parity.json --provider codex --out my-migration/adapter
parity validate --project my-migration/parity.json --adapter my-migration/adapter
```

Generation consumes model quota. It receives contracts, notes, and observed diagnostics. Scenario source and business assertions remain in the trusted parent. Review generated source and limitations, then run independent acceptance after generation.

## Development

```sh
make setup
make check
make test
make demo
```

CI covers macOS, Linux, and an installed wheel outside the checkout. Reports and private run state belong in ignored `.parity/`. Installed examples contain the source, plans, and recorded model responses needed for replay. Older full transcripts are retained in Git history.

Read [architecture](docs/ARCHITECTURE.md), [security boundaries](docs/SECURITY_AND_LIMITATIONS.md), and [example semantics](docs/CONTRACT_AND_SEMANTICS.md) for the supported guarantees.
