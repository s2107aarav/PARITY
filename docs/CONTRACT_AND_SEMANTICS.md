# Contract and semantics

The bundled schemas live under `parity/examples/contracts/{commerce,transfer}/v{1,2}.json` and come from the FastAPI applications as OpenAPI 3.1. Notes supplement what schemas cannot express. `make contracts` regenerates these examples; it does not reset databases. User-owned contracts and scenarios are described in [Projects](PROJECTS.md).

## Commerce V1

Checkout takes nonempty `items[{sku,quantity}]`, positive integer `amount`, `payment_token` and a required `Idempotency-Key`. Amounts are minor units; COFFEE costs 1250 and begins with 30 units, TEA costs 750 and begins with 20. Duplicate SKU lines aggregate for stock checks while retaining input order in the response. No real payment data is used.

Successful checkout is HTTP 201 with `{id:int,status:"paid",amount,items}`. Replaying a successful key/body returns the current order, including a cancelled order, still with 201. Reusing that key with a different body is 409 KEY_REUSED. Rejected attempts do not consume a key permanently; a corrected request may use it again.

| Condition | V1 response |
|---|---|
| Invalid schema, missing/invalid key or invalid pagination | 422 `{error:"INVALID"}` |
| Product absent | 404 `{error:"UNKNOWN_SKU"}` |
| Insufficient stock | 409 `{error:"OUT_OF_STOCK"}` |
| Amount differs from catalog total | 422 `{error:"AMOUNT_MISMATCH"}` |
| Token other than simulated `ok` | 402 `{error:"PAYMENT_DECLINED"}` |
| Missing order | 404 `{error:"NOT_FOUND"}` |

Validation precedence is observable: schema, successful-key identity, then each aggregated product's existence/stock in first-appearance order, then amount, then payment. PARITY's first generated candidate got the stock-versus-amount case wrong. A real model-authored revision added an initial read-only preflight, once per new workflow, and checkpointed completion so retries do not recheck already-reserved stock.

Retrieval and listing expose paid and cancelled orders. Listing uses positive page, page_size 1..100 and ascending stable legacy creation IDs. Cancellation is idempotent and restores stock/refunds once. There is no shipping or post-fulfillment cancellation policy in this fixture.

## Commerce V2

1. Create UUID draft with `{lines:[{product_code,units}],total_minor}` and an idempotency key. It validates product and amount.
2. Reserve stock once per order. Duplicate calls return the same held reservation.
3. Capture simulated payment with `{order_ref,total_minor,token}` and an idempotency key. Both order identity and key prevent another charge. Changed input on an existing payment identity conflicts.
4. Confirm only when reservation and payment exist.
5. Void, when required, releases held stock, refunds a captured charge once, and marks cancelled in one V2-local transaction.

Drafts and compensated failures remain inspectable internal records. They must not appear as successful V1 orders. V2 listing uses a stable sequence cursor and returns internal records. The generated adapter instead enumerates only its published ID mappings, fetches current orders, sorts stable legacy IDs and slices pages. This is a bounded, deliberately simple pagination conversion. The pinned generated artifact caps selected orders at 80 and mappings at 10,000; excess returns PENDING rather than incomplete success.

V2 errors use `{problem:{kind}}`. The plan/code maps PRODUCT_MISSING, BAD_TOTAL, DECLINED, INSUFFICIENT_STOCK, KEY_CONFLICT, MISSING_RESOURCE and INVALID_INPUT to the corresponding V1 status/code.

## Retry and compensation policy

A 503 can occur **after** a V2 effect commits. The adapter persists full legacy input identity, upstream keys, order reference and workflow stage. It returns `503 {error:"PENDING"}` on uncertainty. Retrying the same key/body resumes, including after process restart. A changed pending body conflicts. It never compensates a payment whose outcome is still ambiguous.

Permanent rejection after draft creation enters a durable compensation stage. The original mapped error is retained before void. If void fails or its response is lost, the adapter stays pending; another retry repeats idempotent void and then returns the original business error. Successful cancellation uses the same repeat-safe void semantics. A completed rejected attempt may begin a fresh generation under the same legacy key.

This provides **at-least-once calls with durable deduplication**, not globally exactly-once execution. There is no cross-service transaction or background recovery daemon. A client that abandons a pending request can leave a workflow pending until explicitly retried. During that period V2 may have a stock hold or charge that V1's atomic operation would not expose; temporal atomic visibility is an explicit unsupported guarantee.

## Independent equivalence

Replay seeds equivalent isolated states and applies the same logical client actions. Only order IDs normalize through a one-to-one correspondence learned from paired successful responses. IDs must be positive integers, stable and nonaliased. No amount, status, error, item, list total or stock value is ignored. There are no timestamps to blanket-ignore.

Final-state checks require one charge of exactly the order amount, one equal refund iff cancelled, no orphan ledger rows, no unsettled charged/reserved draft at scenario completion, and `initial stock = available + paid-order units`. Compensated uncharged drafts are allowed only as internal audit records. They cannot mask money, stock or visible-order mismatches.

## Transfer

V1 atomically creates `{id:int,title,body,status:"open"}`. V2 creates `{ref:UUID,summary,state:"draft",comments:[]}`, then sets the sole initial comment and publishes. Title maps to summary; body maps to the one comment. Full legacy fingerprints prevent description changes from bypassing V2's summary-only idempotency. Publish response loss and restart must converge to one issue and one comment. The transfer fixture intentionally has no update, delete, list or authentication surface.
