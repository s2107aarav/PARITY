"""Small issue-tracker transfer fixture: atomic ticket → draft + comment + publish."""

import uuid
from typing import Annotated

from fastapi import FastAPI, Header
from pydantic import Field

from apps.common import ApiError, StrictModel, fingerprint, setup
from parity.storage import Store

ShortText = Annotated[str, Field(min_length=1, max_length=200)]


class Ticket(StrictModel):
    title: ShortText
    body: ShortText


class Issue(StrictModel):
    summary: ShortText


class Comment(StrictModel):
    text: ShortText


class TicketResult(Ticket):
    id: int
    status: str


def create_v1(database: str):
    app = FastAPI(title="Tickets V1", version="1.0.0")
    store = Store(database)
    app.state.store = store
    setup(app, 1)

    @app.post(
        "/tickets", status_code=201, operation_id="create_ticket", response_model=TicketResult
    )
    def create(body: Ticket, idempotency_key: str = Header(min_length=1, max_length=120)):
        """Atomic ticket creation. Same key/body returns same integer ID, different body is 409 KEY_REUSED."""
        with store.transaction() as tx:
            prior = tx.get("requests", idempotency_key)
            if prior:
                if prior["digest"] != fingerprint(body.model_dump()):
                    raise ApiError(409, "KEY_REUSED")
                return tx.get("tickets", prior["id"])
            oid = tx.next_id("tickets")
            row = {"id": oid, "status": "open", **body.model_dump()}
            tx.put("tickets", oid, row)
            tx.put(
                "requests", idempotency_key, {"id": oid, "digest": fingerprint(body.model_dump())}
            )
            return row

    @app.get("/tickets/{ticket_id}", operation_id="get_ticket", response_model=TicketResult)
    def get(ticket_id: int):
        with store.transaction() as tx:
            row = tx.get("tickets", ticket_id)
            if row is None:
                raise ApiError(404, "NOT_FOUND")
            return row

    return app


def create_v2(database: str):
    app = FastAPI(title="Issues V2", version="2.0.0")
    store = Store(database)
    app.state.store = store
    setup(app, 2)

    def find(tx, oid):
        row = tx.get("issues", oid)
        if row is None:
            raise ApiError(404, "MISSING_RESOURCE")
        return row

    @app.post("/issues", status_code=201, operation_id="create_issue")
    def create(body: Issue, idempotency_key: str = Header(min_length=1, max_length=160)):
        """Creates unpublished UUID issue. Same key and summary is idempotent, conflicting key is KEY_CONFLICT."""
        with store.transaction() as tx:
            prior = tx.get("requests", idempotency_key)
            if prior:
                if prior["summary"] != body.summary:
                    raise ApiError(409, "KEY_CONFLICT")
                return find(tx, prior["id"])
            oid = str(uuid.uuid4())
            row = {"ref": oid, "summary": body.summary, "state": "draft", "comments": []}
            tx.put("issues", oid, row)
            tx.put("requests", idempotency_key, {"id": oid, "summary": body.summary})
            return row

    @app.post("/issues/{issue_ref}/comments", operation_id="add_initial_comment")
    def comment(issue_ref: str, body: Comment):
        """Exactly one initial description comment. Repeating same text is safe; different text is 409 KEY_CONFLICT."""
        with store.transaction() as tx:
            row = find(tx, issue_ref)
            if row["comments"] and row["comments"] != [body.text]:
                raise ApiError(409, "KEY_CONFLICT")
            row["comments"] = [body.text]
            tx.put("issues", issue_ref, row)
            return row

    @app.post("/issues/{issue_ref}/publish", operation_id="publish_issue")
    def publish(issue_ref: str):
        """Requires initial comment; idempotently publishes. A 503 after commit is ambiguous: retry safely."""
        with store.transaction() as tx:
            row = find(tx, issue_ref)
            if not row["comments"]:
                raise ApiError(409, "COMMENT_REQUIRED")
            row["state"] = "open"
            tx.put("issues", issue_ref, row)
        app.state.faults.hit("publish.after")
        return row

    @app.get("/issues/{issue_ref}", operation_id="get_issue")
    def get(issue_ref: str):
        with store.transaction() as tx:
            return find(tx, issue_ref)

    return app
