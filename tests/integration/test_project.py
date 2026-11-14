import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from parity.agent import Agent
from parity.project import Project
from parity.relay import relay_app
from parity.sandbox import AdapterProcess
from parity.serving import Server

SOURCE = """def handle(ctx, method, path, body, query, headers):
    with ctx.lock():
        if headers.get("authorization"):
            return 500, {"error": "credential reached worker"}
        result = ctx.request(method, path, body=body, query=query, headers={k: v for k, v in headers.items() if k == "idempotency-key"})
        return ctx.respond(result.status, result.body, result.headers)
"""


class Item(BaseModel):
    id: int
    quantity: int


@pytest.fixture
def project(tmp_path, monkeypatch):
    servers = []
    config = {"name": "inventory", "allow_writes": True}
    for name in ("old", "new"):
        app = FastAPI()
        app.state.items = {}
        app.state.lose = False

        @app.get("/items", response_model=list[Item])
        def items(request: Request):
            return list(request.app.state.items.values())

        @app.post("/items", response_model=Item, status_code=201)
        def create(request: Request, body: dict, idempotency_key: str = Header()):
            row = request.app.state.items.setdefault(
                idempotency_key,
                {"id": len(request.app.state.items) + 1, "quantity": body["quantity"]},
            )
            if request.app.state.lose:
                request.app.state.lose = False
                return JSONResponse({"error": "lost"}, status_code=503)
            return row

        @app.delete("/items/{item_id}", status_code=204)
        def delete(item_id: int):
            return Response(status_code=204)

        @app.get("/restricted", response_class=PlainTextResponse, status_code=401)
        def restricted():
            return PlainTextResponse("Unauthorized", status_code=401, headers={"Retry-After": "60"})

        @app.get("/tags")
        def tags(request: Request):
            return {"tags": request.query_params.getlist("tag")}

        @app.get("/empty")
        def empty():
            return Response(status_code=200, media_type="application/json")

        @app.get("/null")
        def null():
            return JSONResponse(None)

        @app.get("/text-json")
        def text_json():
            return PlainTextResponse('{ "literal": true }')

        @app.get("/json-string")
        def json_string():
            return Response('"text value"', media_type="Application/JSON")

        @app.post("/reset", status_code=205)
        def reset():
            return Response(status_code=205)

        @app.get("/echo/{tail:path}")
        def echo(request: Request):
            return {
                "path": request.scope["raw_path"].decode("ascii"),
                "authorization": request.headers.getlist("authorization"),
            }

        server = Server(app)
        servers.append(server)
        schema = tmp_path / f"{name}.json"
        schema.write_text(json.dumps(app.openapi()))
        config[name] = {
            "schema": schema.name,
            "base_url": server.url,
            "headers": {"Authorization": f"Bearer ${{{name.upper()}_TOKEN}}"},
        }
        monkeypatch.setenv(f"{name.upper()}_TOKEN", f"private-{name}-credential")
    (tmp_path / "parity.json").write_text(json.dumps(config))
    (tmp_path / "notes.md").write_text(
        "Preserve the existing routes, JSON fields, statuses and headers. Each request uses an idempotency key."
    )
    (tmp_path / "scenarios.py").write_text(
        'def test_create(case):\n    case.request("POST", "/items", json={"quantity": 2}, headers={"Idempotency-Key": case.run_id})\n'
    )
    value = Project(tmp_path / "parity.json")
    value.test_servers = servers
    try:
        yield value
    finally:
        for server in reversed(servers):
            server.close()


def adapter(project, source=SOURCE):
    path = project.root / "adapter.py"
    path.write_text(source)
    return path


@pytest.mark.parametrize("name", ["test_workflow", "setup", "teardown", "check_effects"])
@pytest.mark.parametrize(
    "declaration,body", [("async def", "pass"), ("def", "yield 1"), ("async def", "yield 1")]
)
def test_scenarios_cannot_pass_without_running_deferred_functions(project, name, declaration, body):
    project.root.joinpath("scenarios.py").write_text(
        f"def test_sync(case):\n    pass\n{declaration} {name}(case):\n    {body}\n"
    )
    with pytest.raises(ValueError, match=f"{name} must be a synchronous function"):
        project.doctor()


@pytest.mark.parametrize("name", ["test_wrapped", "setup", "teardown", "check_effects"])
@pytest.mark.parametrize(
    "declaration,body",
    [
        ("async def", "raise AssertionError('unexecuted')"),
        ("def", "yield 1"),
        ("async def", "yield 1"),
    ],
)
def test_wrappers_cannot_hide_deferred_scenario_work(project, name, declaration, body):
    project.root.joinpath("scenarios.py").write_text(
        f"{declaration} deferred(case):\n    {body}\n"
        f"def test_sync(case):\n    pass\ndef {name}(case):\n    return deferred(case)\n"
    )
    result = project.validate()
    assert result["passed"] < result["total"]
    failed = [row for row in result["scenarios"] if row["status"] == "fail"]
    assert all("must execute synchronously" in row["mismatch"] for row in failed)
    assert result["business_effects"] != "checked"


def test_validation_executes_scenario_module_once(project):
    project.root.joinpath("scenarios.py").write_text(
        "from pathlib import Path\n"
        'counter = Path(__file__).with_name("imports")\n'
        'counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")\n'
        'def test_import(case):\n    assert counter.read_text() == "1"\n'
    )
    assert project.validate()["passed"] == 1
    assert project.root.joinpath("imports").read_text() == "1"


def test_scenario_loader_uses_current_source_when_timestamp_and_size_match(project):
    import os
    import py_compile

    path = project.root / "scenarios.py"
    path.write_text("def test_value(case):\n    assert True \n")
    py_compile.compile(str(path), doraise=True)
    original = path.stat()
    path.write_text("def test_value(case):\n    assert False\n")
    assert path.stat().st_size == original.st_size
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert project.validate()["passed"] == 0


def test_dataclass_annotations_work_and_temporary_modules_are_cleaned_up(project):
    import sys

    before = {name for name in sys.modules if name.startswith("parity_user_scenarios_")}
    project.root.joinpath("scenarios.py").write_text("""from __future__ import annotations
from dataclasses import dataclass
from typing import get_type_hints
import sys
class Value:
    pass
@dataclass
class Record:
    value: Value
def test_record(case):
    assert __name__ in sys.modules
    assert get_type_hints(Record)["value"] is Value
    assert isinstance(Record(Value()).value, Value)
""")
    assert project.doctor()["scenarios"] == ["test_record"]
    assert project.validate()["passed"] == 1
    assert {name for name in sys.modules if name.startswith("parity_user_scenarios_")} == before


def test_failed_scenario_import_cleans_up_temporary_module(project):
    import sys

    before = {name for name in sys.modules if name.startswith("parity_user_scenarios_")}
    project.root.joinpath("scenarios.py").write_text('raise ValueError("import failed")\n')
    with pytest.raises(ValueError, match="import failed"):
        project.doctor()
    assert {name for name in sys.modules if name.startswith("parity_user_scenarios_")} == before


def test_external_api_validation_and_effect_coverage(project):
    source = adapter(project)
    project.root.joinpath("scenarios.py").write_text("""def test_create(case):
    case.request("POST", "/items", json={"quantity": 2}, headers={"Idempotency-Key": case.run_id})
def check_effects(case):
    assert len(case.old.get("/items").json()) == len(case.upstream.get("/items").json())
""")
    result = project.validate(source)
    assert result["passed"] == result["total"] == 1
    assert result["business_effects"] == "checked"
    assert "private-new-credential" not in Path(result["report"]).read_text()
    assert project.workflows(source)[0]["status"] == "completed"
    assert "authorization" not in project.workflows(source)[0]["request"]["headers"]


@pytest.mark.parametrize("scenario_passes,expected", [(False, "not_checked"), (True, "failed")])
def test_effect_report_distinguishes_skipped_and_failed_checks(project, scenario_passes, expected):
    project.root.joinpath("scenarios.py").write_text(f"""def test_workflow(case):
    assert {scenario_passes!r}, "workflow failed before business checks"
def check_effects(case):
    raise AssertionError("business invariant failed")
""")
    result = project.validate()
    assert result["passed"] == 0
    assert result["business_effects"] == expected
    assert result["scenarios"][0]["business_effects"] == expected


def test_pending_request_can_be_inspected_and_retried(project):
    source = adapter(project)
    project.test_servers[1].app.state.lose = True
    result = project.validate(source)
    assert result["passed"] == 0
    pending = project.workflows(source)[0]
    assert pending["status"] == "pending"
    assert project.retry(source, pending["id"])["status"] == 201
    assert project.workflows(source)[0]["status"] == "completed"
    assert len(project.test_servers[1].app.state.items) == 1


def test_retry_rejects_a_tampered_request_target(project, monkeypatch):
    source = adapter(project)
    monkeypatch.setattr(
        project,
        "workflows",
        lambda _: [
            {
                "id": "invalid",
                "status": "pending",
                "request": {
                    "method": "POST",
                    "path": "https://other.example/items",
                    "headers": {"idempotency-key": "key"},
                },
            }
        ],
    )
    with pytest.raises(ValueError, match="invalid replay request"):
        project.retry(source, "invalid")


def test_worker_preserves_empty_text_headers_and_repeated_query(project, tmp_path):
    source = adapter(project)
    worker = AdapterProcess(source, project.test_servers[1].url, tmp_path / "worker")
    try:
        with httpx.Client(base_url=worker.url, trust_env=False) as client:
            deleted = client.delete("/items/1")
            assert deleted.status_code == 204 and deleted.content == b""
            rejected = client.get("/restricted")
            assert rejected.status_code == 401 and rejected.text == "Unauthorized"
            assert rejected.headers["retry-after"] == "60"
            assert client.get("/tags", params=[("tag", "a"), ("tag", "b")]).json() == {
                "tags": ["a", "b"]
            }
            assert client.get("/echo/a%2Fb%20c").json()["path"] == "/echo/a%2Fb%20c"
            empty = client.get("/empty")
            assert empty.status_code == 200 and empty.content == b""
            null = client.get("/null")
            assert null.status_code == 200 and null.content == b"null"
            assert client.get("/text-json").text == '{ "literal": true }'
            assert client.get("/json-string").json() == "text value"
            reset = client.post("/reset")
            assert reset.status_code == 205 and reset.content == b""
            for literal in ("NaN", "Infinity", "-Infinity"):
                invalid = client.post(
                    "/items", content=literal, headers={"content-type": "application/json"}
                )
                assert invalid.status_code == 422 and invalid.json() == {"error": "INVALID"}
    finally:
        worker.close()


def test_relay_preserves_encoded_paths_and_owns_credentials(project):
    relay = Server(
        relay_app(project.test_servers[1].url, headers={"Authorization": "Bearer configured"})
    )
    try:
        with httpx.Client(base_url=relay.url, trust_env=False) as client:
            response = client.get("/echo/a%2Fb%20c", headers={"authorization": "Bearer wrong"})
            assert response.json() == {
                "path": "/echo/a%2Fb%20c",
                "authorization": ["Bearer configured"],
            }
    finally:
        relay.close()


def test_relay_keeps_cookies_per_caller_and_preserves_repeated_headers():
    from contextlib import ExitStack

    app = FastAPI()

    @app.get("/cookies")
    def cookies(request: Request):
        response = JSONResponse(
            {"cookie": request.headers.get("cookie"), "tags": request.headers.getlist("x-tag")}
        )
        response.set_cookie("first", "one")
        response.set_cookie("second", "two")
        response.headers["connection"] = "x-private"
        response.headers["x-private"] = "private"
        return response

    @app.post("/encoded")
    async def encoded(request: Request):
        return {
            "encoding": request.headers.get("content-encoding"),
            "body": (await request.body()).decode(),
        }

    with ExitStack() as stack:
        upstream = Server(app)
        stack.callback(upstream.close)
        relay = Server(relay_app(upstream.url))
        stack.callback(relay.close)
        for _ in range(2):
            with httpx.Client(base_url=relay.url, trust_env=False) as client:
                response = client.get("/cookies", headers=[("x-tag", "a"), ("x-tag", "b")])
                assert response.json() == {"cookie": None, "tags": ["a", "b"]}
                assert len(response.headers.get_list("set-cookie")) == 2
                assert "x-private" not in response.headers
        with httpx.Client(base_url=relay.url, trust_env=False) as client:
            response = client.get("/cookies", headers={"cookie": "caller=own"})
            assert response.json()["cookie"] == "caller=own"
            encoded = client.post(
                "/encoded", content=b"encoded", headers={"content-encoding": "custom"}
            )
            assert encoded.json() == {"encoding": "custom", "body": "encoded"}


def test_schema_validation_catches_invalid_adapter_type(project):
    source = adapter(
        project,
        SOURCE.replace(
            "return ctx.respond(result.status, result.body, result.headers)",
            'return 201, {"id": 1, "quantity": True}',
        ),
    )
    result = project.validate(source)
    assert result["passed"] == 0
    assert "quantity" in result["scenarios"][0]["mismatch"]


def test_generation_uses_external_scenarios_and_can_resume_before_source(project):
    from parity.plans import MigrationPlan

    plan = MigrationPlan(
        schema_version=1,
        revision=1,
        domain="inventory",
        assumptions=[],
        mappings=[
            {
                "legacy_operation": "POST /items",
                "target_operations": ["POST /items"],
                "field_mappings": {"quantity": "quantity"},
                "evidence": [
                    {"document": "old", "pointer": "#/paths/~1items/post", "confidence": "static"}
                ],
            }
        ],
        workflow=[
            {
                "name": "create",
                "operation": "POST /items",
                "depends_on": [],
                "retry": "same key",
                "compensation": "none",
            }
        ],
        errors=[],
        idempotency_strategy="Preserve the caller's original key for upstream deduplication.",
        identifier_strategy="Preserve upstream integer IDs.",
        pagination_strategy="Not used.",
        limitations=["Declared staging workflow only."],
    )

    class Interrupted:
        def complete(self, *args):
            raise KeyboardInterrupt

    first = Agent(
        project.root / "old.json",
        project.root / "new.json",
        project.root / "notes.md",
        Interrupted(),
        project.root / "interrupted",
        project=project,
    )
    with pytest.raises(KeyboardInterrupt):
        first.run()
    actions = iter(
        [
            ("submit_plan", {"plan": plan.model_dump()}),
            ("submit_code", {"source": SOURCE}),
            ("run_tests", {}),
            ("finish", {}),
        ]
    )

    class Provider:
        def complete(self, *args):
            name, arguments = next(actions)
            return {"tool": name, "arguments": json.dumps(arguments), "reason": "test"}

    resumed = Agent(
        project.root / "old.json",
        project.root / "new.json",
        project.root / "notes.md",
        Provider(),
        project.root / "resumed",
        project=project,
        max_calls=4,
    )
    resumed.resume(first.output)
    result = resumed.run()
    assert result["status"] == "public_validation_passed"
    assert result["model_calls"] == 5
    assert result["unmapped_operations"]
