# Public behavioral evidence — ticketing transfer

V1 POST /tickets {title,body}, both strings length 1..200; Idempotency-Key 1..120 chars required. Atomic success 201 {id:positive integer,title,body,status:"open"}. Same key/body returns same ticket; changed title or body is 409 {error:"KEY_REUSED"}. GET /tickets/{ticket_id}: same representation or 404 {error:"NOT_FOUND"}. Invalid input/header is 422 {error:"INVALID"}.

V2 POST /issues {summary}, Idempotency-Key required: 201 {ref:UUID,summary,state:"draft",comments:[]}. Same key/summary safe; changed summary 409 KEY_CONFLICT.
POST /issues/{issue_ref}/comments {text}: sets exactly one initial description comment, repeat same text safe, changed text 409 KEY_CONFLICT.
POST /issues/{issue_ref}/publish: requires comment; sets state "open", repeat safe.
GET /issues/{issue_ref} returns issue. Missing is 404 MISSING_RESOURCE. Error envelope {problem:{kind:CODE}}. Invalid schema 422 INVALID_INPUT.

A legacy create needs three V2 steps. Title maps to summary; body maps to the sole initial comment. A 503 after publication may occur after commit. Return 503 {error:"PENDING"} for ambiguous failures; same-key retry (including after worker restart) must converge to one open issue with exactly one comment. ctx.lock() serializes local sequences; ctx.get/put persists checkpoints; ctx.id_for/upstream_id handles integer ↔ UUID. Fingerprint BOTH legacy fields, as V2 draft idempotency alone does not cover the description. No update/delete/list/auth support is requested.
