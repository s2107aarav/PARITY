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


@pytest.mark.parametrize("invalid", [None, [], ["id"], "id", 42])
def test_id_mapping_documents_must_be_objects(tmp_path, invalid):
    ctx = Context("http://upstream", str(tmp_path / "adapter.db"))
    try:
        ctx.import_ids({"42": "existing"})
        with pytest.raises(ValueError, match="must be a JSON object"):
            ctx.import_ids(invalid)
        assert ctx.upstream_id(42) == "existing"
        assert ctx.id_for("new") == 43
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


def response_document(schema, *, version="3.1.0", media="application/json"):
    return {
        "openapi": version,
        "paths": {
            "/value": {"get": {"responses": {"200": {"content": {media: {"schema": schema}}}}}}
        },
    }


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_response_contract_rejects_non_json_numeric_constants(literal):
    document = response_document({"type": "number"})
    response = httpx.Response(200, content=literal, headers={"content-type": "application/json"})
    with pytest.raises(AssertionError, match="invalid JSON response"):
        validate_response(document, "GET", "/value", response)


def test_contract_media_type_keys_are_case_insensitive():
    from parity.contracts import operations, payload_fields

    document = response_document(
        {"type": "object", "properties": {"value": {"type": "integer"}}},
        media="Application/JSON",
    )
    assert payload_fields(operations(document)["GET /value"], "response") == {
        "value": {"type": "integer", "required": False}
    }
    validate_response(document, "GET", "/value", httpx.Response(200, json={"value": 1}))
    with pytest.raises(AssertionError):
        validate_response(document, "GET", "/value", httpx.Response(200, json={"value": "bad"}))


@pytest.mark.parametrize("media", ["Application/JSON", "application/notjson"])
def test_paired_requests_use_media_types_for_json_comparison(media):
    from types import SimpleNamespace

    from parity.project import Case

    json_media = media == "Application/JSON"
    document = response_document(
        {"type": "object" if json_media else "string"}, media=media.lower()
    )
    settings = SimpleNamespace(allow_writes=False, id_fields=[], compare_headers=["content-type"])
    project = SimpleNamespace(settings=settings, old=document, new=document)
    left_body = '{"a":1,"b":2}' if json_media else "not json"
    right_body = '{ "b":2, "a":1 }' if json_media else "not json"
    with (
        httpx.Client(
            base_url="http://old",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=left_body, headers={"content-type": media})
            ),
        ) as old,
        httpx.Client(
            base_url="http://new",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, text=right_body, headers={"content-type": media.lower()}
                )
            ),
        ) as new,
    ):
        assert Case(project, old, new).request("GET", "/value").new.status_code == 200


def test_plain_text_that_looks_like_json_is_preserved(tmp_path):
    literal = '{ "literal": true }'
    ctx = Context("http://upstream", str(tmp_path / "adapter.db"))
    ctx.http.close()
    ctx.http = httpx.Client(
        base_url="http://upstream",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=literal)),
    )
    try:
        assert ctx.request("GET", "/value").body == literal
    finally:
        ctx.close()


@pytest.mark.parametrize("name", ["nullable", "exclusiveMinimum", "$ref", "schema"])
def test_schema_keyword_property_names_keep_their_constraints(name):
    document = response_document({"type": "object", "properties": {name: {"type": "integer"}}})
    validate_response(document, "GET", "/value", httpx.Response(200, json={name: 1}))
    with pytest.raises(AssertionError):
        validate_response(document, "GET", "/value", httpx.Response(200, json={name: "wrong"}))


@pytest.mark.parametrize(
    "literal", [{"nullable": 1}, {"$ref": "literal"}, {"exclusiveMinimum": True}]
)
def test_literal_schema_values_are_not_rewritten(literal):
    document = response_document({"const": literal, "default": literal})
    validate_response(document, "GET", "/value", httpx.Response(200, json=literal))
    with pytest.raises(AssertionError):
        validate_response(document, "GET", "/value", httpx.Response(200, json={}))


def test_response_reference_siblings_constrain_instead_of_overwriting():
    document = response_document({"$ref": "#/components/schemas/Value", "maxLength": 4})
    document["components"] = {"schemas": {"Value": {"type": "string", "maxLength": 2}}}
    validate_response(document, "GET", "/value", httpx.Response(200, json="ok"))
    with pytest.raises(AssertionError):
        validate_response(document, "GET", "/value", httpx.Response(200, json="bad"))


def test_recursive_schema_and_reference_outside_components():
    node = {
        "type": "object",
        "properties": {
            "value": {"type": "integer"},
            "next": {"$ref": "#/$defs/Node"},
        },
    }
    document = response_document({"$ref": "#/$defs/Node"})
    document["$defs"] = {"Node": node}
    validate_response(document, "GET", "/value", httpx.Response(200, json={"next": {"value": 1}}))
    with pytest.raises(AssertionError):
        validate_response(
            document, "GET", "/value", httpx.Response(200, json={"next": {"value": "bad"}})
        )


def test_nullable_respects_enum_and_openapi_version():
    schema = {"type": "string", "nullable": True, "enum": ["ok"]}
    for version in ("3.0.3", "3.1.0"):
        document = response_document(schema, version=version)
        with pytest.raises(AssertionError):
            validate_response(
                document,
                "GET",
                "/value",
                httpx.Response(200, content=b"null", headers={"content-type": "application/json"}),
            )
    schema.pop("enum")
    validate_response(
        response_document(schema, version="3.0.3"),
        "GET",
        "/value",
        httpx.Response(200, content=b"null", headers={"content-type": "application/json"}),
    )
    with pytest.raises(AssertionError):
        validate_response(
            response_document(schema),
            "GET",
            "/value",
            httpx.Response(200, content=b"null", headers={"content-type": "application/json"}),
        )


@pytest.mark.parametrize("referenced", [False, True])
def test_write_only_fields_are_not_required_in_responses(referenced):
    document = response_document(
        {
            "type": "object",
            "properties": {
                "password": {"type": "string", "writeOnly": True},
                "name": {"type": "string"},
            },
            "required": ["password", "name"],
        }
    )
    if referenced:
        document["components"] = {"schemas": {"Secret": {"type": "string", "writeOnly": True}}}
        document["paths"]["/value"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["properties"]["password"] = {"$ref": "#/components/schemas/Secret"}
    validate_response(document, "GET", "/value", httpx.Response(200, json={"name": "name"}))
    with pytest.raises(AssertionError):
        validate_response(document, "GET", "/value", httpx.Response(200, json={}))


def test_media_range_accepts_text_and_exact_type_takes_precedence():
    document = response_document({"type": "string", "maxLength": 2}, media="text/*")
    validate_response(document, "GET", "/value", httpx.Response(200, text="ok"))
    with pytest.raises(AssertionError):
        validate_response(document, "GET", "/value", httpx.Response(200, text="bad"))
    document["paths"]["/value"]["get"]["responses"]["200"]["content"]["text/plain"] = {
        "schema": {"const": "exact"}
    }
    validate_response(document, "GET", "/value", httpx.Response(200, text="exact"))


def test_runtime_does_not_carry_cookies_between_callers(tmp_path):
    ctx = Context("http://upstream", str(tmp_path / "adapter.db"))
    ctx.http.close()
    ctx.http = httpx.Client(
        base_url="http://upstream",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"cookie": request.headers.get("cookie")},
                headers={"set-cookie": "private=one; Path=/"},
            )
        ),
    )
    try:
        for _ in range(2):
            with ctx.request_scope():
                assert ctx.request("GET", "/value").body == {"cookie": None}
        assert ctx.request("GET", "/value", headers={"cookie": "own=two"}).body == {
            "cookie": "own=two"
        }
    finally:
        ctx.close()


def test_default_response_reference_and_literal_examples():
    document = response_document({"type": "string", "examples": [{"$ref": "literal"}]})
    responses = document["paths"]["/value"]["get"]["responses"]
    document["components"] = {"responses": {"Fallback": responses.pop("200")}}
    responses["default"] = {"$ref": "#/components/responses/Fallback"}
    validate_response(document, "GET", "/value", httpx.Response(400, json="error"))
    with pytest.raises(AssertionError):
        validate_response(document, "GET", "/value", httpx.Response(400, json=12))
