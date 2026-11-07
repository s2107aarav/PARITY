# Public behavioral evidence — commerce

## V1
Money is integer minor units. Seed: COFFEE costs 1250, stock 30; TEA costs 750, stock 20.
POST /checkout body items[{sku,quantity}], amount, payment_token; Idempotency-Key required (1..120 chars).
Success 201: {id:positive integer,status:"paid",amount,items}. Same successful key/body returns current order (including cancelled), always 201. Different body on a successful key: 409 {error:"KEY_REUSED"}.
GET /orders/{id}: 200 legacy order; missing 404 {error:"NOT_FOUND"}.
GET /orders?page=1&page_size=2: {orders:[...],total,page,page_size}, ascending creation order, includes cancelled orders. page>=1 and page_size 1..100; invalid is 422 INVALID.
POST /orders/{id}/cancel: 200 cancelled order, repeat safe, refund once and restore stock.
GET /inventory/{sku}: {sku,available}. Missing 404 UNKNOWN_SKU.
Checkout failures: unknown SKU 404 UNKNOWN_SKU; out of stock 409 OUT_OF_STOCK; wrong amount 422 AMOUNT_MISMATCH; token other than "ok" 402 PAYMENT_DECLINED; invalid schema or missing header 422 INVALID. Failure body always {error:CODE}. No business effects on rejection.
Duplicate SKU lines are legal and their quantities must be aggregated for stock. Items remain in original order in responses.

## V2
POST /orders {lines:[{product_code,units}],total_minor}; Idempotency-Key; 201 returns {order_ref:UUID,phase:"draft",lines,total_minor,sequence:int}. Validates catalog and amount. Same key/body returns existing order, different body 409 KEY_CONFLICT.
POST /inventory/reservations {order_ref}: holds stock once per order; safe to repeat. 409 INSUFFICIENT_STOCK.
POST /payments {order_ref,total_minor,token}; Idempotency-Key; captures simulated payment once per order AND per key. Requires reservation. A committed payment retried with the same body/key returns same charge. Token != "ok" gives 402 DECLINED with no charge.
POST /orders/{order_ref}/confirm: requires held reservation + payment; idempotently changes phase to "confirmed". V1 paid corresponds to confirmed.
POST /orders/{order_ref}/void: one local transaction releases held stock, refunds a charge if present, and changes phase to "cancelled"; safe to repeat. Also used to compensate rejected drafts. No separate refund/release HTTP calls are needed.
GET /orders/{order_ref}: order; GET /orders?cursor=0&limit=2: {data:[orders in ascending sequence],next_cursor:final sequence or null}. Includes drafts and compensated drafts, which are internal audit records and must not become legacy orders.
GET /stock/{product_code}: {product_code,units_available}.
V2 error envelope {problem:{kind:CODE}}. PRODUCT_MISSING=404, BAD_TOTAL=422, DECLINED=402, INSUFFICIENT_STOCK=409, KEY_CONFLICT=409, MISSING_RESOURCE=404, INVALID_INPUT=422.

## Supported failure policy
Any step may return 503 before or AFTER commit. The latter is an ambiguous lost step response. For temporary failures the adapter returns 503 {error:"PENDING"}; retrying the same legacy key/body must resume safely, including after adapter process restart. Do NOT compensate an ambiguous payment. Permanent rejection after draft creation requires void before reporting the mapped final error; failed compensation returns pending and resumes on retry. No false success.
Durable workflow fingerprints must cover the full legacy request including token. Persist upstream refs before further steps and publish legacy IDs only for successfully confirmed workflows. IDs are positive integers and must survive restart. Identifier gaps are allowed; order listing stays stable and includes only successful legacy workflows.
Concurrent duplicate checkout must produce one charge and one inventory debit. Use ctx.lock() around read-modify-write sequences. Only one local adapter host is supported. V1 temporal atomic visibility during a pending V2 saga is NOT preservable; PENDING is an explicitly documented extension, and eventual reconciliation requires retry. Unsupported: shipping, multi-currency, external writers, noncompensable payments, auth migration, unbounded listings.

## Public validation
Agents may inspect specs/docs and issue bounded probes to fresh local services. Public checks exercise an ordinary checkout and retrieval, inventory, declined payment compensation and retry after a lost payment response. Hidden evaluation is not accessible. Passing public checks does not prove all supported workflows.
