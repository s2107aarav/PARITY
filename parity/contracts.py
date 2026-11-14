"""Conservative OpenAPI delta: static evidence is never labeled semantic proof."""

import json
from pathlib import Path

METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or not str(value.get("openapi", "")).startswith("3.")
        or not isinstance(value.get("paths"), dict)
        or not isinstance(value.get("info"), dict)
    ):
        raise ValueError("expected an OpenAPI 3.x JSON document with paths")
    return value


def pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def resolve_pointer(doc, ref: str):
    if not ref.startswith("#/"):
        raise ValueError("expected a local JSON pointer")
    node = doc
    try:
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, list):
                if not key.isdecimal():
                    raise ValueError("JSON pointer array index must be nonnegative")
                node = node[int(key)]
            else:
                node = node[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unresolved JSON pointer: {ref}") from exc
    return node


def resolve(doc, node, seen=(), *, expand_schemas=True):
    if isinstance(node, list):
        return [resolve(doc, value, seen, expand_schemas=expand_schemas) for value in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str):
            raise ValueError("schema references must be strings")
        if ref in seen:
            return {"$ref": ref}
        if not ref.startswith("#/"):
            raise ValueError("external schema references are not supported")
        result = resolve_pointer(doc, ref)
        resolved = resolve(doc, result, (*seen, ref), expand_schemas=expand_schemas)
        siblings = resolve(
            doc,
            {key: value for key, value in node.items() if key != "$ref"},
            seen,
            expand_schemas=expand_schemas,
        )
        if isinstance(resolved, bool):
            return siblings if resolved and siblings else resolved
        return {**resolved, **siblings} if siblings else resolved
    return {
        k: v
        if k in {"default", "example", "enum", "const", "value"}
        or (k == "examples" and isinstance(v, list))
        or (k == "schema" and not expand_schemas)
        else {
            (name.lower() if k == "content" else name): resolve(
                doc, schema, seen, expand_schemas=expand_schemas
            )
            for name, schema in v.items()
        }
        if k
        in {
            "properties",
            "patternProperties",
            "$defs",
            "definitions",
            "dependentSchemas",
            "schemas",
            "responses",
            "headers",
            "examples",
            "content",
            "paths",
        }
        and isinstance(v, dict)
        else resolve(doc, v, seen, expand_schemas=expand_schemas)
        for k, v in node.items()
    }


def operations(doc, *, expand_schemas=True):
    result = {}
    for path, raw_entry in doc["paths"].items():
        entry = resolve(doc, raw_entry, expand_schemas=expand_schemas)
        for method, op in entry.items():
            if method not in METHODS:
                continue
            operation = {**op, "pointer": f"#/paths/{pointer(path)}/{method}"}
            if "servers" not in operation and "servers" in entry:
                operation["servers"] = entry["servers"]
            parameters = {
                (param["in"], param["name"]): param
                for param in [*entry.get("parameters", []), *op.get("parameters", [])]
            }
            if parameters:
                operation["parameters"] = list(parameters.values())
            result[f"{method.upper()} {path}"] = operation
    return result


def schema_fields(node, prefix=""):
    if not isinstance(node, dict):
        return {}
    result = {}
    for name, spec in node.get("properties", {}).items():
        path = prefix + name
        result[path] = {
            "type": spec.get("type", "composite")
            if isinstance(spec, dict)
            else "any"
            if spec
            else "never",
            "required": name in node.get("required", []),
        }
        result.update(schema_fields(spec, path + "."))
        if isinstance(spec, dict) and spec.get("type") == "array":
            result.update(schema_fields(spec.get("items", {}), path + "[]."))
    return result


def payload_fields(operation, section):
    if section == "request":
        schema = (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
    else:
        responses = operation.get("responses", {})
        success: dict = next((v for k, v in responses.items() if k.startswith("2")), {})
        schema = success.get("content", {}).get("application/json", {}).get("schema", {})
    return schema_fields(schema)


def analyze(old: dict, new: dict):
    left, right = operations(old), operations(new)
    changes = []
    for section in ("security", "servers", "securitySchemes"):
        before, after = (
            (doc.get("components", {}) if section == "securitySchemes" else doc).get(section)
            for doc in (old, new)
        )
        location = "#/components/" + section if section == "securitySchemes" else "#/" + section
        if before != after:
            changes.append(
                {
                    "kind": section + "_changed",
                    "operation": "*",
                    "before": before,
                    "after": after,
                    "impact": "review",
                    "certainty": "static",
                    "evidence": [
                        {"document": "old", "pointer": location},
                        {"document": "new", "pointer": location},
                    ],
                }
            )
    for name, op in left.items():
        match = (
            name
            if name in right
            else next(
                (
                    k
                    for k, v in right.items()
                    if op.get("operationId") and v.get("operationId") == op["operationId"]
                ),
                None,
            )
        )
        sources = [{"document": "old", "pointer": op["pointer"]}]
        if match is None:
            changes.append(
                {
                    "kind": "removed_operation",
                    "operation": name,
                    "evidence": sources,
                    "certainty": "static",
                    "semantic_mapping": "unknown; inspect behavior",
                }
            )
            continue
        candidate = right[match]
        sources.append({"document": "new", "pointer": candidate["pointer"]})
        if match != name:
            changes.append(
                {
                    "kind": "operation_path_changed",
                    "operation": name,
                    "successor": match,
                    "evidence": sources,
                    "certainty": "static",
                }
            )
        old_op, new_op = resolve(old, op), resolve(new, candidate)
        for section in ("request", "response"):
            before_fields, after_fields = (
                payload_fields(old_op, section),
                payload_fields(new_op, section),
            )
            for field in sorted(before_fields.keys() | after_fields.keys()):
                before, after = before_fields.get(field), after_fields.get(field)
                if before == after:
                    continue
                kind = (
                    "field_added"
                    if before is None
                    else "field_removed"
                    if after is None
                    else "field_changed"
                )
                changes.append(
                    {
                        "kind": kind,
                        "section": section,
                        "operation": name,
                        "field": field,
                        "before": before,
                        "after": after,
                        "type_changed": bool(before and after and before["type"] != after["type"]),
                        "required_changed": bool(
                            before and after and before["required"] != after["required"]
                        ),
                        "evidence": sources,
                        "certainty": "static",
                    }
                )
        for section in ("requestBody", "responses", "parameters", "security", "servers"):
            a, b = old_op.get(section), new_op.get(section)
            if a != b:
                entry = {
                    "kind": section + "_changed",
                    "operation": name,
                    "successor": match,
                    "before": a,
                    "after": b,
                    "evidence": sources,
                    "certainty": "static",
                }
                changes.append(entry)
        if "parameters" in old_op:
            params = old_op["parameters"] + new_op.get("parameters", [])
            if any(x.get("name") in {"page", "cursor", "page_size", "limit"} for x in params):
                changes.append(
                    {
                        "kind": "pagination_review",
                        "operation": name,
                        "evidence": sources,
                        "certainty": "requires_behavioral_evidence",
                    }
                )
    added = sorted(set(right) - set(left))
    return {
        "schema_version": 1,
        "old": old["info"],
        "new": new["info"],
        "changes": changes,
        "added_operations": added,
        "schema_fields": {
            "old": {
                k: schema_fields(resolve(old, v))
                for k, v in old.get("components", {}).get("schemas", {}).items()
            },
            "new": {
                k: schema_fields(resolve(new, v))
                for k, v in new.get("components", {}).get("schemas", {}).items()
            },
        },
        "uncertainties": [
            "Matching operationId is a candidate mapping, not proof of behavior.",
            "Atomicity, retry recovery, money and stock effects require docs and bounded probes.",
            "Removed operations plus added primitives may require a one-to-many workflow; semantic evidence required.",
        ],
    }
