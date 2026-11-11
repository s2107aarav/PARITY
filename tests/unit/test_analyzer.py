import json

import pytest
from pydantic import ValidationError

from parity.contracts import analyze, load, operations, resolve, resolve_pointer
from parity.plans import MigrationPlan, validate_plan
from parity.resources import examples
from parity.sandbox import check_source
from parity.usage import has_output_tokens


def test_real_contract_delta_includes_semantic_uncertainty():
    old, new = (load(examples() / "contracts/commerce" / x) for x in ("v1.json", "v2.json"))
    delta = analyze(old, new)
    removed = [x["operation"] for x in delta["changes"] if x["kind"] == "removed_operation"]
    assert "POST /checkout" in removed
    assert "POST /payments" in delta["added_operations"]
    assert any(x["kind"] == "pagination_review" for x in delta["changes"])
    assert delta["schema_fields"]["old"]["Checkout"]["items[].quantity"]["required"]
    assert all(x.get("evidence") for x in delta["changes"])
    assert len(delta["uncertainties"]) >= 2


def test_external_refs_rejected():
    with pytest.raises(ValueError):
        resolve({}, {"$ref": "https://evil.invalid/schema"})


def test_plan_rejects_cycles_and_unknown_fields():
    with pytest.raises(ValidationError):
        MigrationPlan.model_validate({"schema_version": 1, "revision": 1, "hidden_override": True})
    value = json.loads((examples() / "artifacts/generated/commerce-live-002/plan.json").read_text())
    value["workflow"][0]["depends_on"] = [value["workflow"][0]["name"]]
    with pytest.raises(ValidationError, match="dependency order"):
        MigrationPlan.model_validate(value)


def test_explicit_field_and_required_deltas():
    def document(schema):
        return {
            "info": {"title": "fixture", "version": "1"},
            "paths": {
                "/example": {
                    "post": {
                        "operationId": "example",
                        "requestBody": {"content": {"application/json": {"schema": schema}}},
                    }
                }
            },
        }

    old = document({"type": "object", "properties": {"value": {"type": "integer"}}, "required": []})
    new = document(
        {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}
    )
    fields = [x for x in analyze(old, new)["changes"] if x["kind"] == "field_changed"]
    assert len(fields) == 1 and fields[0]["type_changed"] and fields[0]["required_changed"]


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef handle(*args): return os.environ",
        "def handle(ctx, *args): return ctx.http.get('x')",
        "def handle(ctx, *args): return ctx.request_scope()",
        "def handle(ctx, *args): return ctx.record_result('key', 'completed')",
        "def handle(*args): return open('/etc/passwd').read()",
        "def handle(*args): return (1).__class__",
        "from parity.runtime import Store\ndef handle(*args): return 200, {}",
        "from parity.runtime import Path\ndef handle(*args): return 200, {}",
        "import parity.runtime\ndef handle(*args): return 200, {}",
        "from parity.runtime import *\ndef handle(*args): return 200, {}",
        "from .json import loads\ndef handle(*args): return 200, {}",
    ],
)
def test_source_capability_filter(source):
    with pytest.raises(ValueError):
        check_source(source)


def test_shipped_generation_has_real_provenance():
    path = examples() / "artifacts/generated/commerce-live-001"
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["provenance"] == "real-authenticated-model-run"
    assert (path / "plan.json").is_file() and (path / "adapter.py").is_file()
    calls = list((path / "calls").glob("*/usage.json"))
    assert any(has_output_tokens(json.loads(x.read_text())) for x in calls)


@pytest.mark.parametrize(
    "record, expected",
    [
        ({}, False),
        ({"usage": None}, False),
        ({"usage": {}}, False),
        ({"usage": {"output_tokens": None}}, False),
        ({"usage": {"output_tokens": 0}}, False),
        ({"usage": {"output_tokens": 7}}, True),
    ],
)
def test_optional_provider_usage(record, expected):
    assert has_output_tokens(record) is expected


def test_missing_operation_ids_do_not_match_unrelated_routes():
    old = {"info": {}, "paths": {"/old": {"get": {"responses": {}}}}}
    new = {"info": {}, "paths": {"/new": {"get": {"responses": {}}}}}
    assert analyze(old, new)["changes"][0]["kind"] == "removed_operation"


def test_inherited_parameters_are_compared_and_operation_overrides_apply():
    def document(required):
        return {
            "info": {},
            "paths": {
                "/items": {
                    "parameters": [{"in": "query", "name": "cursor", "required": required}],
                    "get": {"responses": {}},
                    "head": {
                        "responses": {},
                        "parameters": [{"in": "query", "name": "cursor", "required": False}],
                    },
                }
            },
        }

    delta = analyze(document(False), document(True))
    changed = [row for row in delta["changes"] if row["kind"] == "parameters_changed"]
    assert [row["operation"] for row in changed] == ["GET /items"]
    assert "HEAD /items" in operations(document(True))


def test_reference_siblings_and_nested_arrays_are_retained():
    doc = {"components": {"schemas": {"Value": {"type": "string"}}}}
    node = {"$ref": "#/components/schemas/Value", "description": "Current value"}
    assert resolve(doc, [node]) == [{"type": "string", "description": "Current value"}]


def test_plan_evidence_can_point_into_parameter_array():
    base = examples() / "contracts/transfer"
    old, new = load(base / "v1.json"), load(base / "v2.json")
    plan = MigrationPlan.model_validate_json(
        (examples() / "artifacts/generated/transfer-live-001/plan.json").read_text()
    )
    evidence = plan.mappings[0].evidence[0]
    evidence.document = "old"
    evidence.pointer = "#/paths/~1tickets/post/parameters/0"
    assert validate_plan(plan, old, new) is plan
    with pytest.raises(ValueError, match="unresolved JSON pointer"):
        resolve_pointer(old, "#/paths/~1tickets/post/parameters/99")


def test_contract_loader_rejects_non_object(tmp_path):
    path = tmp_path / "contract.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="OpenAPI"):
        load(path)
