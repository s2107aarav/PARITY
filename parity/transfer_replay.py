"""Independent second-domain acceptance predicates, not supplied to the agent."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from apps.transfer import create_v1, create_v2
from parity.sandbox import AdapterProcess, WorkerFailure, digest
from parity.serving import Server

SCENARIOS = ("ordinary", "retry", "publish_loss_restart", "conflict", "validation", "concurrent")


def evaluate(source: Path, output: Path, *, trusted=False):
    output.mkdir(parents=True, exist_ok=True)
    results = []
    checker_hash = digest(Path(__file__))
    for index, scenario in enumerate(SCENARIOS):
        root = output / f"{index:02d}-{scenario}"
        root.mkdir()
        old = Server(create_v1(str(root / "v1.db")))
        new = Server(create_v2(str(root / "v2.db")))
        worker = None
        actions: list[dict] = []
        record = {"scenario": scenario, "seed": 0, "status": "fail"}
        try:
            worker = AdapterProcess(source, new.url, root / "worker", trusted=trusted)
            with (
                httpx.Client(base_url=old.url, trust_env=False, timeout=15) as left,
                httpx.Client(base_url=worker.url, trust_env=False, timeout=15) as right,
            ):
                body = {"title": "Login broken", "body": "Cannot sign in"}
                ids: dict[int, int] = {}

                def post(client, value=body, key="ticket"):
                    return client.post("/tickets", json=value, headers={"Idempotency-Key": key})

                def compare(a, b, actions=actions, ids=ids):
                    actions.append(
                        {
                            "expected_status": a.status_code,
                            "actual_status": b.status_code,
                            "expected": a.json(),
                            "actual": b.json(),
                        }
                    )
                    assert a.status_code == b.status_code, "HTTP mismatch"
                    expected, actual = a.json(), b.json()
                    if "id" in expected:
                        assert type(actual.get("id")) is int and actual["id"] > 0
                        old_id, new_id = expected["id"], actual["id"]
                        assert old_id not in ids or ids[old_id] == new_id, (
                            "unstable ID correspondence"
                        )
                        assert all(
                            key == old_id or value != new_id for key, value in ids.items()
                        ), "aliased ticket IDs"
                        ids[old_id] = new_id
                        expected["id"] = new_id
                    assert expected == actual, "semantic response mismatch"

                if scenario == "publish_loss_restart":
                    expected = post(left)
                    new.app.state.faults.arm("publish.after")
                    pending = post(right)
                    assert pending.status_code == 503 and pending.json() == {"error": "PENDING"}
                    worker.restart()
                    compare(expected, post(right))
                elif scenario == "validation":
                    for value in (
                        {"title": "", "body": "x"},
                        {"title": "a"},
                        {"title": 3, "body": "b"},
                    ):
                        compare(post(left, value), post(right, value))
                    compare(left.get("/tickets/999"), right.get("/tickets/999"))
                elif scenario == "concurrent":
                    with ThreadPoolExecutor(max_workers=6) as pool:
                        a = list(pool.map(lambda _: post(left), range(6)))
                        b = list(pool.map(lambda _: post(right), range(6)))
                    for x, y in zip(a, b, strict=True):
                        compare(x, y)
                    assert len({json.dumps(x.json(), sort_keys=True) for x in b}) == 1, (
                        "unstable concurrent IDs"
                    )
                else:
                    a, b = post(left), post(right)
                    compare(a, b)
                    if scenario == "ordinary":
                        compare(
                            left.get(f"/tickets/{a.json()['id']}"),
                            right.get(f"/tickets/{b.json()['id']}"),
                        )
                    if scenario == "retry":
                        compare(post(left), post(right))
                        assert post(right).json() == b.json(), "unstable ID on retry"
                    if scenario == "conflict":
                        compare(
                            post(left, {**body, "body": "changed"}),
                            post(right, {**body, "body": "changed"}),
                        )
            baseline = list(old.app.state.store.snapshot().get("tickets", {}).values())
            candidate = list(new.app.state.store.snapshot().get("issues", {}).values())
            assert len(baseline) == len(candidate), "missing/duplicate issues"
            expected_state = sorted((x["title"], x["body"], x["status"]) for x in baseline)
            actual_state = []
            for issue in candidate:
                assert len(issue["comments"]) == 1, "initial comment missing/duplicated"
                actual_state.append((issue["summary"], issue["comments"][0], issue["state"]))
            assert expected_state == sorted(actual_state), "final issue business effects differ"
            record["status"] = "pass"
            record["isolation"] = worker.isolation
        except Exception as exc:
            error = exc
            if worker:
                try:
                    worker.ensure_alive()
                except WorkerFailure as failure:
                    error = failure
            record["failure_kind"] = (
                "worker_infrastructure"
                if isinstance(error, WorkerFailure)
                else "semantic_or_trial_error"
            )
            record["mismatch"] = f"{type(error).__name__}: {error}"
        finally:
            record["actions"] = actions
            record["after"] = {
                "v1": old.app.state.store.snapshot(),
                "v2": new.app.state.store.snapshot(),
            }
            if worker:
                worker.close()
            old.close()
            new.close()
        (root / "result.json").write_text(json.dumps(record, indent=2))
        results.append(record)
    assert digest(Path(__file__)) == checker_hash, "transfer checker changed"
    report = {
        "suite": "transfer",
        "seed": 0,
        "artifact_sha256": digest(source),
        "checker_sha256": checker_hash,
        "scenarios": results,
        "passed": sum(x["status"] == "pass" for x in results),
        "total": len(results),
    }
    (output / "evaluation.json").write_text(json.dumps(report, indent=2))
    return report
