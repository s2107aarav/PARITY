import json

import httpx
import pytest

from parity.comparison import compare_json, validate_response
from parity.runtime import Context, UpstreamError


@pytest.mark.parametrize(
    "expected,actual",
    [
        (1, True),
        (False, 0),
        ({"items": [{"quantity": 1}]}, {"items": [{"quantity": True}]}),
        ([1], [1, 2]),
    ],
)
def test_comparison_rejects_type_and_structure_changes(expected, actual):
    with pytest.raises(AssertionError, match=r"\$"):
        compare_json(expected, actual)


@pytest.mark.parametrize(
    "status,body", [(204, b""), (401, b"Unauthorized"), (429, b'{"error":"limited"}')]
)
def test_transport_preserves_empty_and_text_responses(tmp_path, status, body):
    ctx = Context("http://upstream", str(tmp_path / "adapter.db"))
    ctx.http.close()
    ctx.http = httpx.Client(
        base_url="http://upstream",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, content=body, headers={"Retry-After": "60"})
        ),
    )
    try:
        with ctx.lock():
            result = ctx.request("GET", "/items")
            assert result.status == status and result.headers["retry-after"] == "60"
            if status == 204:
                assert ctx.call("DELETE", "/items/1") is None
            else:
                with pytest.raises(UpstreamError) as error:
                    ctx.call("GET", "/items")
                assert error.value.status == status
    finally:
        ctx.close()


def test_response_schema_rejects_boolean_integer():
    document = {
        "paths": {
            "/items/{id}": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"quantity": {"type": "integer"}},
                                        "required": ["quantity"],
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    with pytest.raises(AssertionError, match="quantity"):
        validate_response(document, "GET", "/items/1", httpx.Response(200, json={"quantity": True}))


def test_imported_ids_are_bijective_and_advance_allocator(tmp_path):
    ctx = Context("http://upstream", str(tmp_path / "adapter.db"))
    try:
        ctx.import_ids({"42": "existing"})
        assert ctx.upstream_id(42) == "existing"
        assert ctx.id_for("new") == 43
        with pytest.raises(ValueError, match="conflict"):
            ctx.import_ids({"42": "different"})
        with pytest.raises(ValueError):
            ctx.import_ids({"1": "same", "2": "same"})
        with pytest.raises(ValueError):
            ctx.import_ids({"1": "one", "01": "another"})
    finally:
        ctx.close()


@pytest.mark.parametrize(
    "schema,value,valid",
    [
        (True, 1, True),
        (False, 1, False),
        ({"type": "number", "minimum": 1, "exclusiveMinimum": True}, 1, False),
        ({"type": "integer", "nullable": True}, None, True),
    ],
)
def test_openapi_schema_variants(schema, value, valid):
    document = {
        "paths": {
            "/value": {
                "get": {"responses": {"200": {"content": {"application/json": {"schema": schema}}}}}
            }
        }
    }
    response = httpx.Response(
        200,
        content=b"null" if value is None else json.dumps(value).encode(),
        headers={"content-type": "application/json"},
    )
    if valid:
        validate_response(document, "GET", "/value", response)
    else:
        with pytest.raises(AssertionError):
            validate_response(document, "GET", "/value", response)


def test_request_budget_survives_relocking_and_nested_lock(tmp_path):
    ctx = Context("http://upstream", str(tmp_path / "adapter.db"))
    ctx.http.close()
    ctx.http = httpx.Client(
        base_url="http://upstream",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    try:
        with ctx.request_scope():
            with ctx.lock(), ctx.lock():
                for _ in range(40):
                    ctx.call("GET", "/value")
            with ctx.lock():
                for _ in range(40):
                    ctx.call("GET", "/value")
            with pytest.raises(UpstreamError, match="503"):
                ctx.call("GET", "/value")
        with ctx.request_scope():
            assert ctx.call("GET", "/value") == {}
    finally:
        ctx.close()
