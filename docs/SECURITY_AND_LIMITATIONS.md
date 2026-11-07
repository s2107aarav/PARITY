# Security and limitations

## Hardening policy

The worker explicitly caps AnyIO at8 threads. Linux RLIMIT_AS is2GiB to accommodate virtual mappings and pthread stack reservations; this is not a2GiB RSS allowance. A separate768MiB sampled process-group RSS watchdog includes bubblewrap descendants, with possible transient overshoot. macOS uses the sampled RSS bound without RLIMIT_AS. CPU25s, wall120s, FD128 and file32MiB bounds remain. Monitor failure kills execution; a limit kill is infrastructure failure, not semantic success. The actual exercised security path is macOS Seatbelt; Linux remains unverified and retains its documented weaker network boundary. No sandbox means refusal, never implicit trusted mode.

Historical generation artifacts and release manifests retain their recorded bytes. `artifacts/pins.json` identifies the current engine, fixed client and generated adapters; engine fingerprints must be refreshed when that source changes. Public packages are separate copies built by `scripts/package_release.py`: home paths, hostnames and provider account/thread identifiers are redacted with original/public hash pairs. Generated Python/plans remain byte-identical. Raw originals and a pre-change archive are private under ignored `.parity/hardening`; the original PARITY.zip is not the public release. Redacted transcripts have different hashes from their raw originals.

This is a local, production-inspired engineering project, not a production migration gateway. Generated Python is treated as untrusted. No real money, external payment gateway, production account or customer data is involved.

## Exercised boundary

On macOS a generated module runs under Seatbelt in a separate process. The worker receives an empty credential environment, a copied plugin/runtime, its own writable state directory, read-only Python/dependency roots and narrow system libraries. Repository contents, the manual reference and checker files are outside its read policy. Ancestor directory metadata/listing is allowed to make Python path traversal work; file contents remain denied. A test runs a harmless canary under the **actual policy**, independently of AST filtering, and observes denial for checker, reference and Codex authentication files.

Network outbound is restricted to the controlled numeric-loopback V2 port; the only inbound port is the worker's V1-facing port. The plugin runtime refuses absolute upstream URLs, uses no environment proxies, caps each request at 80 upstream calls and gives each upstream request a three-second timeout. Evaluation clients have bounded timeouts. The worker has a 120-second wall limit, 25 CPU seconds, 128 descriptors and 32 MiB per output file. Linux also has an address-space limit; macOS RSS is sampled against 768 MiB as a **soft** bound with possible transient overshoot. Body decoding is limited to 64 KiB after transport read; this is not a streaming upload defense.

Source AST filtering rejects dangerous imports/builtins, introspection and direct private capabilities. It is defense in depth, **not a Python security proof**. The OS boundary matters because Python sandboxing by syntax alone is insufficient.

Linux bubblewrap restricts filesystem/process visibility but currently shares host networking. That fallback was not exercised here and is weaker for network containment. Unsupported platforms without a sandbox refuse untrusted execution. `trusted=True` exists for explicit engineering fixtures only; default generated validation never uses it. Docker execution was not tested because the daemon and Compose CLI plugin were unavailable. No unrestricted generated-code fallback is silently substituted.

## Model and evaluator trust

The provider process is trusted integration code with authenticated model access. It runs from an empty temporary working directory with user configuration/native shell, app, browser and multi-agent features disabled. Structured responses select typed PARITY tools. Native tool execution events are audited. This does not make the provider CLI itself an adversarial sandbox; repository owners and the trusted parent can still read files.

Only public specs, notes, bounded probe results, the model's own artifacts and public diagnostics are supplied to the agent. Private checker and reference code are never supplied or mounted into generated workers. The commerce evaluator hashes itself before/after, restores itself on detected mutation, and verifies the client hash. Exact pinned hashes also run in tests. These protections detect accidental/tampered artifacts inside the chosen process boundary; they cannot protect against a malicious repository owner changing both code and pins.

Public diagnostic failures are legitimate repair feedback. Hidden acceptance predicates remain outside the tool surface. The same private checker evaluates reference, rules and generated commerce candidates. Synthetic model outputs appear only in parser/budget unit tests and never count as AI generation evidence.

No API key, authorization header or account credential is written into generated workers. HTTP trace serialization redacts credential-like dictionary fields. Saved model prompts contain only artificial fixture tokens (`ok`, `declined`) and public documents. Raw provider logs can contain account quota messages and local paths; review before public release. Dollar cost and undisclosed model identity remain null, not estimates.

## Explicit unsupported guarantees

- Multi-host coordination, distributed transactions, background reconciliation and high throughput.
- V1 temporal atomic visibility while a V2 saga remains pending.
- External writers changing inventory between preflight and reservation.
- Irreversible/nonrefundable financial effects, shipping, real credentials or payment data.
- Auth migrations, streaming, arbitrary remote schema references and unrestricted API discovery.
- Unbounded pagination or arbitrary APIs without fixture/probe/checker registration.
- Statistical superiority from one model trial and a small deterministic scenario suite.

SQLite and PostgreSQL use different locking mechanisms. The PostgreSQL smoke/restart experiment validates selected business paths; it does not establish multi-replica performance or the unexecuted Compose deployment.
