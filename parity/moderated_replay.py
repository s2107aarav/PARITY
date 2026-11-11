"""Sealed private acceptance for the curated approval-receipt challenge."""

import json
from pathlib import Path

import httpx

from apps.moderated import LegacyTicketClient, new_app, old_app
from parity.comparison import compare_json, validate_response
from parity.contracts import load
from parity.resources import examples
from parity.sandbox import AdapterProcess, digest
from parity.serving import Server


def evaluate(source: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    contract = load(examples() / "contracts/moderated/v1.json")
    results = []
    for scenario in (
        "ordinary",
        "approval_loss_restart",
        "publication_loss_restart",
        "key_conflict",
    ):
        root = output / scenario
        root.mkdir()
        old, new = Server(old_app(str(root / "old.db"))), Server(new_app(str(root / "new.db")))
        worker = None
        record = {"scenario": scenario, "status": "fail"}
        try:
            worker = AdapterProcess(source, new.url, root / "worker")
            with (
                httpx.Client(base_url=old.url, trust_env=False) as a,
                httpx.Client(base_url=worker.url, trust_env=False, timeout=15) as b,
            ):
                legacy, candidate = LegacyTicketClient(a), LegacyTicketClient(b)
                expected = legacy.create()
                if "loss" in scenario:
                    new.app.state.faults.arm(
                        "approve.after" if scenario.startswith("approval") else "publish.after"
                    )
                    pending = candidate.create()
                    assert pending.status_code == 503 and pending.json() == {"error": "PENDING"}
                    worker.restart()
                actual = candidate.create()
                record.update(
                    expected=expected.json(), actual=actual.json(), actual_status=actual.status_code
                )
                assert actual.status_code == expected.status_code == 201
                validate_response(contract, "POST", "/tickets", expected)
                validate_response(contract, "POST", "/tickets", actual)
                x, y = expected.json(), actual.json()
                assert type(y["id"]) is int and y["id"] > 0
                x["id"] = y["id"]
                compare_json(x, y)
                compare_json(y, candidate.create().json())
                if scenario == "key_conflict":
                    assert (
                        candidate.create(body="changed").status_code
                        == legacy.create(body="changed").status_code
                        == 409
                    )
                state = new.app.state.store.snapshot()
                issues, approvals = state.get("issues", {}), state.get("approvals", {})
                assert len(issues) == len(approvals) == 1, (
                    "fake/duplicate issue or missing approval"
                )
                issue = next(iter(issues.values()))
                assert issue["state"] == "open" and issue["comments"] == ["Paper jam"]
                assert issue["summary"] == "Printer broken"
                assert set(issues) == set(approvals)
                record["status"] = "pass"
        except Exception as exc:
            record["mismatch"] = f"{type(exc).__name__}: {exc}"
        finally:
            record["after"] = new.app.state.store.snapshot()
            if worker:
                worker.close()
            old.close()
            new.close()
        results.append(record)
    result = {
        "suite": "moderated",
        "checker_sha256": digest(Path(__file__)),
        "source_sha256": digest(source),
        "scenarios": results,
        "passed": sum(r["status"] == "pass" for r in results),
        "total": len(results),
    }
    (output / "evaluation.json").write_text(json.dumps(result, indent=2))
    return result
