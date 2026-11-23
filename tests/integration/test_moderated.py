from fastapi.testclient import TestClient

from apps.moderated import new_app
from parity.moderated_replay import evaluate
from parity.resources import examples


def test_approval_is_real_required_and_idempotent(tmp_path):
    app = new_app(str(tmp_path / "service.db"))
    with TestClient(app) as http:
        issue = http.post(
            "/issues", json={"summary": "Broken"}, headers={"Idempotency-Key": "one"}
        ).json()["ref"]
        assert http.post(f"/issues/{issue}/approve").status_code == 409
        assert http.post(f"/issues/{issue}/publish", json={"receipt": "fake"}).status_code == 409
        http.post(f"/issues/{issue}/comments", json={"text": "Details"})
        app.state.faults.arm("approve.after")
        assert http.post(f"/issues/{issue}/approve").status_code == 503
        approval = http.post(f"/issues/{issue}/approve").json()
        assert http.post(f"/issues/{issue}/approve").json() == approval
        assert (
            http.post(f"/issues/{issue}/publish", json={"receipt": approval["receipt"]}).json()[
                "state"
            ]
            == "open"
        )
    app.state.store.close()


def test_fresh_checker_rejects_fabricated_success(tmp_path):
    source = tmp_path / "fake.py"
    source.write_text(
        'def handle(ctx, method, path, body, query, headers):\n    return 201, {"id":1,"title":"Printer broken","body":"Paper jam","status":"open"}\n'
    )
    result = evaluate(source, tmp_path / "negative")
    assert result["passed"] == 0 and result["total"] == 4


def test_generated_adapter_preserves_approval_and_publication(tmp_path):
    source = examples() / "artifacts/generated/moderated-release-001/adapter.py"
    result = evaluate(source, tmp_path / "moderated")
    assert result["passed"] == result["total"] == 4, result


def test_checker_rejects_wrong_retry_status_with_an_unchanged_body(tmp_path):
    artifact = examples() / "artifacts/generated/moderated-release-001/adapter.py"
    source = tmp_path / "wrong_retry_status.py"
    source.write_text(
        artifact.read_text().replace("return 201, state['result']", "return 200, state['result']")
    )
    result = evaluate(source, tmp_path / "wrong-retry-status")
    assert result["passed"] == 0
    assert all(
        row["mismatch"] == "AssertionError: retry HTTP status mismatch"
        for row in result["scenarios"]
    )
