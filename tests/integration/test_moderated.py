from fastapi.testclient import TestClient

from apps.moderated import new_app
from parity.moderated_replay import evaluate


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
