# Projects

A project contains `parity.json`, two OpenAPI JSON documents, `notes.md`, and a Python scenario module. All configured file paths are relative to `parity.json`.

## Configuration

```json
{
  "version": 1,
  "name": "inventory-upgrade",
  "old": {
    "schema": "old.json",
    "base_url": "https://old.staging.example.com",
    "headers": {"Authorization": "Bearer ${OLD_TOKEN}"}
  },
  "new": {
    "schema": "new.json",
    "base_url": "https://new.staging.example.com",
    "headers": {"Authorization": "Bearer ${NEW_TOKEN}"}
  },
  "scenarios": "scenarios.py",
  "notes": "notes.md",
  "workspace": ".parity",
  "allow_writes": false,
  "timeout": 10,
  "id_fields": ["id"],
  "compare_headers": ["content-type", "etag", "location", "retry-after"]
}
```

Set credential variables in your shell. PARITY does not load `.env` files or accept credentials embedded in URLs. The trusted relay adds configured new-API headers; generated workers do not receive those credentials or environment variables.

`doctor` checks configuration, files, environment references, and scenario discovery. It loads your Python module but makes no API calls itself. API path prefixes belong in scenario paths. Contracts support OpenAPI 3.x JSON with local references; remote references and YAML are unsupported.

## Scenarios

Every `test_` function receives a fresh `case`. `case.request` sends both requests and compares status, selected headers, and body. Both responses are schema-validated. When testing an adapter, its output must satisfy the old contract.

```python
def test_create_and_read(case):
    created = case.request(
        "POST", "/items",
        json={"quantity": 2},
        headers={"Idempotency-Key": case.run_id},
    )
    case.request(
        "GET", f"/items/{created.old.json()['id']}",
        new_path=f"/items/{created.new.json()['id']}",
    )
```

Use `new_path` or `new_json` for deliberately different raw API requests. Response comparison remains strict. When response shapes change, an adapter supplies the translation.

`id_fields` explicitly permits stable one-to-one correspondence for those field names within a scenario. These fields share one identifier namespace; use separate scenarios for unrelated resources with overlapping IDs. Other values remain unchanged. Identifiers must be integers or strings; booleans are not integers. Leave `id_fields` empty to compare identifiers literally.

Optional `setup(case)` and `teardown(case)` run around every scenario. Teardown runs even after failure; its failures make the scenario fail. Reset disposable data between runs. The old request may create data before the new request fails, and PARITY cannot undo arbitrary APIs automatically.

Scenario modules are trusted owner-written code with host access. The `allow_writes` guard applies to paired requests and the generated-worker relay; direct HTTP calls inside your own Python hooks remain your responsibility.

## Business effects

Define `check_effects(case)` to inspect business effects after a successful scenario. `case.old` accesses the old API. `case.upstream` always accesses the real new API, including when `case.new` points to an adapter. These clients carry their configured credentials.

```python
def check_effects(case):
    old = case.old.get("/test-support/ledger").json()
    new = case.upstream.get("/test-support/ledger").json()
    assert old["charge_count"] == new["charge_count"]
    assert old["total_minor"] == new["total_minor"]
```

These are examples of owner-provided staging endpoints, not endpoints supplied by PARITY. Results cover only the scenarios and assertions you declared. Missing business checks are marked `not_checked`.

## Generation

Describe field transformations, idempotency, compensation, and unsupported cases in `notes.md`. Schemas alone cannot determine these semantics.

```sh
parity generate --project parity.json --provider codex --max-calls 10 --out adapter
parity validate --project parity.json --adapter adapter
```

`ctx.request` returns an object with `status`, `headers`, and `body`. `ctx.respond` returns the adapter's HTTP response. `ctx.get`/`put` persist JSON checkpoints; `id_for`/`upstream_id` maintain correspondence. The original `ctx.call` and `(status, JSON)` response ABI remain supported.

Plans check known operations, dependency order, and plain field references where schemas declare fields. Descriptive transformations and literal status mappings require review. Manifests identify unmapped operations; validation binds the source and plan hashes. The generator never receives your scenario or assertion source.

After interruption or provider failure, resume into a new output directory, even if no source was produced:

```sh
parity generate --project parity.json --resume-artifact adapter --out adapter-resumed
```

The continuation preserves saved history and counters and requires fresh diagnostics before finishing. Its public pass is separate from a subsequent independent acceptance run.

## Existing IDs and recovery

Set `id_mappings` to a JSON filename containing correspondence such as `{"42": "upstream-uuid"}`. Imports reject aliases and conflicts and advance the allocator. Upstream records must already exist; the import does not create business data.

```sh
parity status --project parity.json --adapter adapter
parity retry --project parity.json --adapter adapter --request REQUEST_ID
```

The worker records write requests before dispatch and their HTTP outcome afterward. Server errors and interrupted dispatch remain pending. Retry requires writes to be enabled and the original `Idempotency-Key`. It replays that request against the same artifact and state directory. Check that the migration's idempotency contract permits replay. There is no background retry loop.

State is scoped by adapter source hash. Changing source starts another runtime directory; state is not silently transferred to incompatible code. Keep workspaces private: retry records include request bodies, even though configured credentials and HTTP body traces are excluded.
