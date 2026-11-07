"""MANUALLY AUTHORED transfer calibration fixture, excluded from agent inputs."""

from parity.runtime import UpstreamError


def ticket(ctx, issue):
    return {"id": ctx.id_for(issue["ref"]), "title": issue["summary"],
            "body": issue["comments"][0], "status": "open"}


def handle(ctx, method, path, body, query, headers):
    with ctx.lock():
        try:
            if method == "POST" and path == "/tickets":
                key = headers.get("idempotency-key", "")
                if not key or len(key) > 120 or not isinstance(body, dict) or set(body) != {"title", "body"}:
                    return 422, {"error": "INVALID"}
                if not all(isinstance(v, str) and 1 <= len(v) <= 200 for v in body.values()):
                    return 422, {"error": "INVALID"}
                state = ctx.get("ticket:" + key, {"digest": ctx.digest(body)})
                if state["digest"] != ctx.digest(body):
                    return 409, {"error": "KEY_REUSED"}
                if "ref" not in state:
                    row = ctx.call("POST", "/issues", {"summary": body["title"]},
                                   headers={"Idempotency-Key": key})
                    state["ref"] = row["ref"]
                    ctx.put("ticket:" + key, state)
                ref = state["ref"]
                ctx.call("POST", "/issues/" + ref + "/comments", {"text": body["body"]})
                row = ctx.call("POST", "/issues/" + ref + "/publish")
                return 201, ticket(ctx, row)
            parts = path.strip("/").split("/")
            if method == "GET" and len(parts) == 2 and parts[0] == "tickets":
                ref = ctx.upstream_id(int(parts[1]))
                if ref is None:
                    return 404, {"error": "NOT_FOUND"}
                return 200, ticket(ctx, ctx.call("GET", "/issues/" + ref))
            return 404, {"error": "NOT_FOUND"}
        except (ValueError, TypeError):
            return 422, {"error": "INVALID"}
        except UpstreamError as exc:
            if exc.status >= 500:
                return 503, {"error": "PENDING"}
            kind = exc.data.get("problem", {}).get("kind")
            codes = {"MISSING_RESOURCE": (404, "NOT_FOUND"), "KEY_CONFLICT": (409, "KEY_REUSED"),
                     "INVALID_INPUT": (422, "INVALID")}
            status, code = codes.get(kind, (502, "UPSTREAM_ERROR"))
            return status, {"error": code}
