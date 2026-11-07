"""One curated fresh challenge: publication now requires a durable approval receipt."""

import uuid

from apps.common import ApiError, StrictModel
from apps.transfer import create_v1, create_v2


class Approval(StrictModel):
    receipt: str


def old_app(database):
    app = create_v1(database)
    app.title = "Moderated tickets V1"
    return app


def new_app(database):
    app = create_v2(database)
    app.title = "Moderated issues V3"
    app.version = "3.0.0"
    # Retain draft/comment/read implementations; replace the publication capability.
    app.router.routes = [
        r for r in app.router.routes if getattr(r, "operation_id", None) != "publish_issue"
    ]
    store = app.state.store

    @app.post("/issues/{issue_ref}/approve", operation_id="approve_issue")
    def approve(issue_ref: str):
        """Requires initial comment. Idempotently returns a stable approval receipt.
        A 503 may occur AFTER the receipt commits; retry this endpoint to reconcile.
        """
        with store.transaction() as tx:
            issue = tx.get("issues", issue_ref)
            if not issue:
                raise ApiError(404, "MISSING_RESOURCE")
            if not issue["comments"]:
                raise ApiError(409, "COMMENT_REQUIRED")
            result = tx.get("approvals", issue_ref) or {
                "receipt": str(uuid.uuid4()),
                "issue_ref": issue_ref,
            }
            tx.put("approvals", issue_ref, result)
        app.state.faults.hit("approve.after")
        return result

    @app.post("/issues/{issue_ref}/publish", operation_id="publish_approved_issue")
    def publish(issue_ref: str, body: Approval):
        """Requires the exact receipt returned by approve. Idempotently publishes."""
        with store.transaction() as tx:
            issue = tx.get("issues", issue_ref)
            if not issue:
                raise ApiError(404, "MISSING_RESOURCE")
            approval = tx.get("approvals", issue_ref)
            if not approval or body.receipt != approval["receipt"]:
                raise ApiError(409, "APPROVAL_REQUIRED")
            issue["state"] = "open"
            tx.put("issues", issue_ref, issue)
        app.state.faults.hit("publish.after")
        return issue

    return app


def public_check(client, service, worker):
    body = {"title": "Public moderated ticket", "body": "Description"}
    response = client.post("/tickets", json=body, headers={"Idempotency-Key": "public"})
    assert response.status_code == 201 and response.json()["body"] == body["body"], response.text


class LegacyTicketClient:
    """Fixed challenge workflow: no knowledge of approval or successor routes."""

    def __init__(self, http):
        self.http = http

    def create(self, key="challenge", title="Printer broken", body="Paper jam"):
        return self.http.post(
            "/tickets", headers={"Idempotency-Key": key}, json={"title": title, "body": body}
        )
