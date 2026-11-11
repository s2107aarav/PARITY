"""HTTP contract validation and type-aware JSON comparison."""

import copy
import re

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from parity.contracts import operations


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
    candidates = operations(document)
    exact = candidates.get(f"{method.upper()} {path}")
    if exact is not None:
        return exact
    for name, operation in candidates.items():
        verb, template = name.split(" ", 1)
        pattern = "[^/]+".join(re.escape(part) for part in re.split(r"\{[^}]+\}", template))
        if verb == method.upper() and re.fullmatch(pattern, path):
            return operation
    raise AssertionError(f"{method} {path}: operation absent from contract")


def _schema(value):
    if isinstance(value, list):
        return [_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _schema(item) for key, item in value.items() if key != "nullable"}
    for bound in ("Minimum", "Maximum"):
        keyword = "exclusive" + bound
        if type(value.get(keyword)) is bool:
            result.pop(keyword)
            if value[keyword] and bound.lower() in result:
                result[keyword] = result.pop(bound.lower())
    if value.get("nullable"):
        return {"anyOf": [result, {"type": "null"}]}
    return result


def validate_response(document, method, path, response):
    operation = operation_for(document, method, path)
    responses = operation.get("responses", {})
    status = str(response.status_code)
    definition = responses.get(status, responses.get(status[0] + "XX", responses.get("default")))
    if definition is None:
        raise AssertionError(f"{method} {path}: undocumented HTTP {status}")
    content = definition.get("content", {})
    if response.status_code in (204, 304) or method.upper() == "HEAD":
        if response.content:
            raise AssertionError(f"{method} {path}: HTTP {status} must have an empty body")
        return
    if not content:
        if response.content:
            raise AssertionError(f"{method} {path}: response body absent from contract")
        return
    media = response.headers.get("content-type", "").split(";", 1)[0].lower()
    definition = content.get(
        media, content.get("application/*+json") if media.endswith("+json") else None
    )
    if definition is None:
        definition = content.get("*/*")
    if definition is None:
        raise AssertionError(f"{method} {path}: undocumented content type {media!r}")
    if media == "application/json" or media.endswith("+json"):
        try:
            data = response.json()
        except ValueError as exc:
            raise AssertionError(f"{method} {path}: invalid JSON response") from exc
    else:
        data = response.text
    schema = {
        "allOf": [_schema(copy.deepcopy(definition.get("schema", {})))],
        "components": _schema(document.get("components", {})),
    }
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
