"""HTTP contract validation and type-aware JSON comparison."""

import copy
import re

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from parity.contracts import operations, resolve_pointer


def _invalid_json_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _is_json_media_type(value):
    media = value.split(";", 1)[0].strip().lower()
    return media == "application/json" or media.endswith("+json")


def compare_json(expected, actual, path="$"):
    if isinstance(expected, dict) and isinstance(actual, dict):
        if expected.keys() != actual.keys():
            raise AssertionError(
                f"{path}: object keys differ: expected {sorted(expected)}, got {sorted(actual)}"
            )
        for key, value in expected.items():
            compare_json(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            raise AssertionError(f"{path}: expected {len(expected)} entries, got {len(actual)}")
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            compare_json(left, right, f"{path}[{index}]")
    else:
        numeric = type(expected) in (int, float) and type(actual) in (int, float)
        if (not numeric and type(expected) is not type(actual)) or expected != actual:
            raise AssertionError(
                f"{path}: expected {expected!r} ({type(expected).__name__}), got {actual!r} ({type(actual).__name__})"
            )


def operation_for(document, method, path):
    path = path.split("?", 1)[0]
    candidates = operations(document, expand_schemas=False)
    exact = candidates.get(f"{method.upper()} {path}")
    if exact is not None:
        return exact
    for name, operation in candidates.items():
        verb, template = name.split(" ", 1)
        pattern = "[^/]+".join(re.escape(part) for part in re.split(r"\{[^}]+\}", template))
        if verb == method.upper() and re.fullmatch(pattern, path):
            return operation
    raise AssertionError(f"{method} {path}: operation absent from contract")


def _write_only(value, document):
    seen = set()
    while isinstance(value, dict):
        if value.get("writeOnly"):
            return True
        ref = value.get("$ref")
        if not document or not isinstance(ref, str) or ref in seen:
            break
        seen.add(ref)
        value = resolve_pointer(document, ref)
    return False


def _schema(value, *, legacy=True, document=None):
    if not isinstance(value, dict):
        return value
    result = copy.deepcopy(value)
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        if isinstance(result.get(key), dict):
            result[key] = {
                name: _schema(item, legacy=legacy, document=document)
                for name, item in result[key].items()
            }
    for key in (
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "contains",
        "items",
        "additionalItems",
        "unevaluatedItems",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    ):
        if key in result:
            result[key] = _schema(result[key], legacy=legacy, document=document)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if key in result:
            result[key] = [_schema(item, legacy=legacy, document=document) for item in result[key]]
    properties = result.get("properties", {})
    if "required" in result:
        result["required"] = [
            name for name in result["required"] if not _write_only(properties.get(name), document)
        ]
    if not legacy:
        return result
    result.pop("nullable", None)
    for bound in ("Minimum", "Maximum"):
        keyword = "exclusive" + bound
        if type(value.get(keyword)) is bool:
            result.pop(keyword)
            if value[keyword] and bound.lower() in result:
                result[keyword] = result.pop(bound.lower())
    if value.get("nullable") and "type" in result:
        result["type"] = [result["type"], "null"]
    return result


def _document_schemas(value, *, legacy, document):
    if isinstance(value, list):
        return [_document_schemas(item, legacy=legacy, document=document) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _schema(item, legacy=legacy, document=document)
        if key == "schema"
        else {
            name: _schema(schema, legacy=legacy, document=document) for name, schema in item.items()
        }
        if key == "schemas"
        else item
        if key in {"example", "examples", "default", "enum", "const"}
        else _document_schemas(item, legacy=legacy, document=document)
        for key, item in value.items()
    }


def validate_response(document, method, path, response):
    operation = operation_for(document, method, path)
    responses = operation.get("responses", {})
    status = str(response.status_code)
    definition = responses.get(status, responses.get(status[0] + "XX", responses.get("default")))
    if definition is None:
        raise AssertionError(f"{method} {path}: undocumented HTTP {status}")
    content = definition.get("content", {})
    if response.status_code in (204, 205, 304) or method.upper() == "HEAD":
        if response.content:
            raise AssertionError(f"{method} {path}: HTTP {status} must have an empty body")
        return
    if not content:
        if response.content:
            raise AssertionError(f"{method} {path}: response body absent from contract")
        return
    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    definition = content.get(
        media, content.get("application/*+json") if media.endswith("+json") else None
    )
    if definition is None:
        definition = content.get(media.split("/", 1)[0] + "/*") if "/" in media else None
    if definition is None:
        definition = content.get("*/*")
    if definition is None:
        raise AssertionError(f"{method} {path}: undocumented content type {media!r}")
    if _is_json_media_type(media):
        try:
            data = response.json(parse_constant=_invalid_json_constant)
        except ValueError as exc:
            raise AssertionError(f"{method} {path}: invalid JSON response") from exc
    else:
        data = response.text
    legacy = str(document.get("openapi", "3.0")).startswith("3.0")
    schema = _document_schemas(document, legacy=legacy, document=document)
    schema["allOf"] = [_schema(definition.get("schema", {}), legacy=legacy, document=document)]
    error = next(
        iter(
            Draft202012Validator(
                schema, format_checker=FormatChecker(), registry=Registry()
            ).iter_errors(data)
        ),
        None,
    )
    if error:
        location = "$" + "".join(
            f"[{key}]" if isinstance(key, int) else f".{key}" for key in error.path
        )
        raise AssertionError(f"{method} {path} HTTP {status} {location}: {error.message}")
