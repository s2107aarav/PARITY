"""User-owned staging projects and paired HTTP scenarios."""

import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from parity.comparison import compare_json, exact_number, require_assertions, validate_response
from parity.contracts import operations, parse
from parity.files import write_json
from parity.http_values import is_json_media_type, media_type
from parity.relay import relay_app
from parity.runtime import REPLAY_HEADERS, Context, redact
from parity.sandbox import AdapterProcess, AdapterSource
from parity.serving import Server
from parity.storage import Store


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Endpoint(Settings):
    schema_path: str = Field(alias="schema")
    base_url: str
    headers: dict[str, str] = Field(default_factory=dict)

    def credentials(self):
        def substitute(match):
            name = match.group(1)
            if name not in os.environ:
                raise ValueError(f"missing environment variable: {name}")
            return os.environ[name]

        return {
            key: re.sub(r"\$\{([A-Za-z_]\w*)\}", substitute, value)
            for key, value in self.headers.items()
        }


class ProjectSettings(Settings):
    version: int = Field(default=1, ge=1, le=1)
    name: str
    old: Endpoint
    new: Endpoint
    scenarios: str = "scenarios.py"
    notes: str = "notes.md"
    workspace: str = ".parity"
    allow_writes: bool = False
    timeout: float = Field(default=10, gt=0, le=60)
    id_fields: list[str] = Field(default_factory=list)
    compare_headers: list[str] = Field(
        default_factory=lambda: ["content-type", "etag", "location", "retry-after"]
    )
    id_mappings: str | None = None
    bind_existing_runtime: bool = False


@dataclass
class Pair:
    old: httpx.Response
    new: httpx.Response


def _invoke(function, case):
    result = function(case)
    if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
        if inspect.iscoroutine(result) or inspect.isgenerator(result):
            result.close()
        raise ValueError("scenario functions must execute synchronously without yielding")


class Case:
    def __init__(self, project, old, new, *, adapter=False, headers=None, upstream=None):
        self.project, self.old, self.new = project, old, new
        self.adapter = adapter
        self.upstream = upstream or new
        self.compare_headers = headers if headers is not None else project.settings.compare_headers
        self.events: list[dict] = []
        self.ids: dict[object, object] = {}
        self.run_id = uuid.uuid4().hex

    def normalize(self, expected, actual):
        if isinstance(expected, dict) and isinstance(actual, dict):
            result = {}
            for key, value in expected.items():
                if key in self.project.settings.id_fields and key in actual:
                    candidate = actual[key]
                    if type(value) not in (int, str) or type(candidate) not in (int, str):
                        raise AssertionError(f"{key}: identifiers must be strings or integers")
                    identity = (type(value).__name__, value)
                    target = (type(candidate).__name__, candidate)
                    if identity in self.ids and self.ids[identity] != target:
                        raise AssertionError(f"{key}: unstable ID correspondence")
                    if any(
                        other != identity and known == target for other, known in self.ids.items()
                    ):
                        raise AssertionError(f"{key}: aliased IDs")
                    self.ids[identity] = target
                    result[key] = candidate
                else:
                    result[key] = self.normalize(value, actual.get(key))
            return result
        if isinstance(expected, list) and isinstance(actual, list):
            return [
                self.normalize(value, actual[index] if index < len(actual) else None)
                for index, value in enumerate(expected)
            ]
        return expected

    def request(self, method, path, *, new_path=None, new_json=None, **kwargs):
        method = method.upper()
        if method not in {"GET", "HEAD", "OPTIONS"} and not self.project.settings.allow_writes:
            raise ValueError(
                "Writes are disabled; use disposable staging data and set allow_writes in parity.json"
            )
        for value in (path, new_path or path):
            if not value.startswith("/") or value.startswith("//") or "://" in value:
                raise ValueError("scenario requests must use relative paths")
        right_kwargs = dict(kwargs)
        if new_json is not None:
            right_kwargs["json"] = new_json
        started = time.perf_counter()
        left = self.old.request(method, path, **kwargs)
        event = {
            "method": method,
            "path": path,
            "new_path": new_path or path,
            "expected_status": left.status_code,
            "actual_status": None,
        }
        self.events.append(event)
        expected_body = validate_response(self.project.old, method, path, left)
        right = self.new.request(method, new_path or path, **right_kwargs)
        event.update(
            actual_status=right.status_code,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        if left.status_code != right.status_code:
            raise AssertionError(
                f"{method} {path}: expected HTTP {left.status_code}, got {right.status_code}"
            )
        actual_body = validate_response(
            self.project.old if self.adapter else self.project.new,
            method,
            path if self.adapter else new_path or path,
            right,
        )
        for header in self.compare_headers:
            expected, actual = left.headers.get_list(header), right.headers.get_list(header)
            if header.lower() == "content-type":
                expected = [media_type(value) for value in expected]
                actual = [media_type(value) for value in actual]
            if expected != actual:
                raise AssertionError(f"{method} {path}: {header} header differs")
        if not left.content and not right.content:
            return Pair(left, right)
        if is_json_media_type(left.headers.get("content-type", "")):
            compare_json(self.normalize(expected_body, actual_body), actual_body)
        else:
            compare_json(left.text, right.text)
        return Pair(left, right)


class Project:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.root = self.path.parent
        self.settings = ProjectSettings.model_validate_json(self.path.read_text())
        for endpoint in (self.settings.old, self.settings.new):
            url = urlsplit(endpoint.base_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or url.port == 0
            ):
                raise ValueError(
                    "endpoint URLs must be HTTP(S) without embedded credentials, query or fragment"
                )
            if url.path not in ("", "/"):
                raise ValueError("put API path prefixes in scenarios, not endpoint base_url")
        old, new = (
            (self.root / endpoint.schema_path).read_bytes()
            for endpoint in (self.settings.old, self.settings.new)
        )
        self.old, self.new = (parse(raw, parse_float=exact_number) for raw in (old, new))
        self.contract_hashes = {
            "old": hashlib.sha256(old).hexdigest(),
            "new": hashlib.sha256(new).hexdigest(),
        }
        self.workspace = (self.root / self.settings.workspace).resolve()

    @contextmanager
    def scenario_module(self):
        path = self.root / self.settings.scenarios
        spec = importlib.util.spec_from_file_location(
            "parity_user_scenarios_" + uuid.uuid4().hex, path
        )
        if spec is None or spec.loader is None:
            raise ValueError("could not load scenario module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            raw = path.read_bytes()
            exec(compile(raw, str(path), "exec"), module.__dict__)
            yield module, self._scenario_tests(module), hashlib.sha256(raw).hexdigest()
        finally:
            sys.modules.pop(spec.name, None)

    @staticmethod
    def _scenario_tests(module):
        tests = [
            (name, value)
            for name, value in vars(module).items()
            if name.startswith("test_") and callable(value)
        ]
        if not tests:
            raise ValueError(
                "scenario module must define at least one test_ function accepting case"
            )
        for name, function in [
            *tests,
            *(
                (name, getattr(module, name, None))
                for name in ("setup", "teardown", "check_effects")
            ),
        ]:
            if (
                inspect.iscoroutinefunction(function)
                or inspect.isgeneratorfunction(function)
                or inspect.isasyncgenfunction(function)
            ):
                raise ValueError(f"{name} must be a synchronous function without yield")
        return tests

    def doctor(self):
        with self.scenario_module() as (module, tests, _):
            return self._doctor(module, tests)

    def _doctor(self, module, tests):
        operations(self.old)
        operations(self.new)
        for endpoint in (self.settings.old, self.settings.new):
            endpoint.credentials()
        if not (self.root / self.settings.notes).is_file():
            raise ValueError("notes file is missing")
        return {
            "project": self.settings.name,
            "scenarios": [name for name, _ in tests],
            "business_effects": "declared"
            if callable(getattr(module, "check_effects", None))
            else "not_checked",
            "allow_writes": self.settings.allow_writes,
            "workspace": str(self.workspace),
        }

    def runtime_root(self, adapter):
        return self.workspace / "runtime" / AdapterSource.read(adapter).sha256[:16]

    def prepare_runtime(self, adapter):
        root = self.runtime_root(adapter)
        database = root / "state/adapter.db"
        target = str(httpx.URL(self.settings.new.base_url).copy_with(path="/"))
        store = Store(str(database))
        try:
            with store.transaction() as tx:
                bound = tx.get("runtime", "upstream")
                if bound is None:
                    if not tx.empty() and not self.settings.bind_existing_runtime:
                        raise ValueError(
                            "Existing adapter state has no upstream binding. Verify its original URL "
                            "before setting bind_existing_runtime in parity.json."
                        )
                    tx.put("runtime", "upstream", target)
                elif bound != target:
                    raise ValueError(
                        "Adapter state belongs to a different upstream URL. Restore the original URL "
                        "or use a separate workspace for the new service."
                    )
        finally:
            store.close()
        if self.settings.id_mappings:
            mappings = json.loads((self.root / self.settings.id_mappings).read_text())
            with_context = Context("http://localhost", str(database), trace_bodies=False)
            try:
                with_context.import_ids(mappings)
            finally:
                with_context.close()
        return root

    def validate(self, adapter=None, output=None):
        require_assertions()
        source = AdapterSource.read(adapter) if adapter else None
        with self.scenario_module() as (module, tests, scenarios_sha256):
            self._doctor(module, tests)
            return self._validate(module, tests, source, output, scenarios_sha256)

    def _validate(self, module, tests, adapter, output, scenarios_sha256):
        output = Path(output) if output else self.workspace / "checks" / uuid.uuid4().hex
        output.mkdir(parents=True, exist_ok=False)
        rows = []
        effects = callable(getattr(module, "check_effects", None))
        with ExitStack() as stack:
            old = stack.enter_context(
                httpx.Client(
                    base_url=self.settings.old.base_url,
                    headers=self.settings.old.credentials(),
                    timeout=self.settings.timeout,
                    trust_env=False,
                )
            )
            target = self.settings.new.base_url
            credentials = self.settings.new.credentials()
            worker = None
            if adapter:
                runtime = self.prepare_runtime(adapter)
                proxy = Server(
                    relay_app(
                        target,
                        headers=credentials,
                        timeout=self.settings.timeout,
                        allow_writes=self.settings.allow_writes,
                    )
                )
                stack.callback(proxy.close)
                worker = AdapterProcess(adapter, proxy.url, runtime, trace_bodies=False)
                stack.callback(worker.close)
                target, credentials = worker.url, {}
            new = stack.enter_context(
                httpx.Client(
                    base_url=target,
                    headers=credentials,
                    timeout=self.settings.timeout,
                    trust_env=False,
                )
            )
            upstream = stack.enter_context(
                httpx.Client(
                    base_url=self.settings.new.base_url,
                    headers=self.settings.new.credentials(),
                    timeout=self.settings.timeout,
                    trust_env=False,
                )
            )
            for name, test in tests:
                case = Case(self, old, new, adapter=bool(adapter), upstream=upstream)
                row = {
                    "scenario": name,
                    "status": "fail",
                    "business_effects": "not_checked",
                    "actions": case.events,
                }
                try:
                    if callable(getattr(module, "setup", None)):
                        _invoke(module.setup, case)
                    _invoke(test, case)
                    if effects:
                        row["business_effects"] = "failed"
                        _invoke(module.check_effects, case)
                        row["business_effects"] = "passed"
                    row["status"] = "pass"
                except Exception as exc:
                    row["mismatch"] = f"{type(exc).__name__}: {exc}"
                    if worker:
                        try:
                            worker.ensure_alive()
                        except RuntimeError as failure:
                            row["failure_kind"] = "worker_infrastructure"
                            row["mismatch"] = str(failure)
                finally:
                    if callable(getattr(module, "teardown", None)):
                        try:
                            _invoke(module.teardown, case)
                        except Exception as exc:
                            row.update(status="fail", teardown_error=f"{type(exc).__name__}: {exc}")
                            row.setdefault("mismatch", f"teardown: {row['teardown_error']}")
                rows.append(row)
        effect_results = {row["business_effects"] for row in rows}
        effect_status = "not_checked"
        if "passed" in effect_results:
            effect_status = "checked" if effect_results == {"passed"} else "partial"
        if "failed" in effect_results:
            effect_status = "failed"
        result = {
            "project": self.settings.name,
            "passed": sum(row["status"] == "pass" for row in rows),
            "total": len(rows),
            "scenarios": rows,
            "business_effects": effect_status,
            "coverage": "only declared scenarios and invariants",
            "adapter_sha256": adapter.sha256 if adapter else None,
            "scenarios_sha256": scenarios_sha256,
            "contracts": self.contract_hashes,
        }
        write_json(output / "evaluation.json", result)
        from parity.report import render_project

        render_project(output, result)
        result["report"] = str(output / "index.html")
        return result

    def probe(self, version, method, path, body, headers):
        if method != "GET" and not self.settings.allow_writes:
            raise ValueError("writes are disabled in project configuration")
        endpoint = getattr(self.settings, version)
        with httpx.Client(
            base_url=endpoint.base_url,
            headers=endpoint.credentials(),
            timeout=self.settings.timeout,
            trust_env=False,
        ) as client:
            response = client.request(method, path, json=body, headers=headers)
            try:
                value = response.json()
            except ValueError:
                value = response.text
            return {"status": response.status_code, "body": redact(value)}

    def workflows(self, adapter):
        database = self.runtime_root(adapter) / "state/adapter.db"
        if not database.exists():
            return []
        store = Store(str(database))
        try:
            return list(store.snapshot().get("workflows", {}).values())
        finally:
            store.close()

    def retry(self, adapter, request_id):
        if not self.settings.allow_writes:
            raise ValueError("writes are disabled in project configuration")
        adapter = AdapterSource.read(adapter)
        record = next(
            (value for value in self.workflows(adapter) if value["id"] == request_id), None
        )
        if not record or record["status"] != "pending":
            raise ValueError("request must identify a pending workflow")
        request = record["request"]
        path = request.get("path", "")
        if (
            request.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}
            or not path.startswith("/")
            or path.startswith("//")
            or "://" in path
            or not REPLAY_HEADERS.issuperset(request.get("headers", {}))
        ):
            raise ValueError("recorded workflow contains an invalid replay request")
        if not request["headers"].get("idempotency-key"):
            raise ValueError("replay requires the original Idempotency-Key")
        omitted = record.get("unreplayed_headers")
        if omitted is None:
            raise ValueError("record predates header checks; replay the original client request")
        if omitted:
            raise ValueError(f"replay cannot reconstruct caller headers: {', '.join(omitted)}")
        runtime = self.prepare_runtime(adapter)
        with ExitStack() as stack:
            proxy = Server(
                relay_app(
                    self.settings.new.base_url,
                    headers=self.settings.new.credentials(),
                    timeout=self.settings.timeout,
                    allow_writes=True,
                )
            )
            stack.callback(proxy.close)
            worker = AdapterProcess(adapter, proxy.url, runtime, trace_bodies=False)
            stack.callback(worker.close)
            if record not in self.workflows(adapter):
                raise ValueError("workflow changed before retry; inspect its current status")
            with httpx.Client(
                base_url=worker.url, timeout=self.settings.timeout, trust_env=False
            ) as client:
                response = client.send(
                    httpx.Request(
                        request["method"],
                        client.base_url.join(path),
                        content=json.dumps(request["body"], allow_nan=False)
                        if request["body"] is not None
                        else None,
                        params=request["query"],
                        headers=request["headers"],
                    )
                )
            return {"id": request_id, "status": response.status_code}


def initialize(directory: Path, *, example=None):
    directory.mkdir(parents=True, exist_ok=True)
    names = ["parity.json", "scenarios.py", "notes.md"]
    if example:
        names += ["old.json", "new.json"]
    targets = [directory / name for name in names]
    if any(path.exists() for path in targets):
        raise ValueError("init will not overwrite an existing project file")
    write_json(
        targets[0],
        {
            "version": 1,
            "name": directory.resolve().name,
            "old": {"schema": "old.json", "base_url": "http://127.0.0.1:8001"},
            "new": {"schema": "new.json", "base_url": "http://127.0.0.1:8002"},
            "scenarios": "scenarios.py",
            "notes": "notes.md",
            "allow_writes": False,
            "id_fields": [],
        },
    )
    targets[1].write_text('def test_list(case):\n    case.request("GET", "/items")\n')
    targets[2].write_text(
        "Describe the supported workflows, field changes, retry behavior, and business invariants for this migration.\n"
    )
    if example:
        import shutil

        from parity.resources import examples

        root = examples() / "contracts/transfer"
        shutil.copyfile(root / "v1.json", directory / "old.json")
        shutil.copyfile(root / "v2.json", directory / "new.json")
        shutil.copyfile(root / "notes.md", directory / "notes.md")
        settings = json.loads(targets[0].read_text())
        settings.update(allow_writes=True, id_fields=["id"])
        write_json(targets[0], settings)
        targets[1].write_text("""def test_create_and_retrieve(case):
    created = case.request(
        "POST", "/tickets",
        json={"title": "Printer broken", "body": "Paper jam"},
        headers={"Idempotency-Key": case.run_id},
    )
    case.request(
        "GET", f"/tickets/{created.old.json()['id']}",
        new_path=f"/tickets/{created.new.json()['id']}",
    )
""")
    return targets[0]
