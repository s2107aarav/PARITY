# Security and supported scope

Use staging endpoints and disposable data you control. Write scenarios opt in through `allow_writes`. Scenario modules are trusted developer code with normal host access; do not load an untrusted scenario file.

Generated adapters are untrusted. AST filtering limits capabilities but is not a Python security proof. macOS uses Seatbelt with explicit interpreter/runtime read roots and one loopback upstream port. Linux uses bubblewrap filesystem, process, and network namespaces with Unix-socket HTTP relays. Generated execution refuses to start without supported isolation.

Worker trial budgets are 120 seconds of lifetime, 25 CPU seconds, 128 descriptors, and 32 MiB per file. Linux limits address space to 2 GiB. A sampled RSS watchdog applies a soft 768 MiB bound; transient overshoot is possible. On Linux it includes worker descendants even when they change process groups. Monitor failure terminates execution. These are evaluation budgets, not settings for a persistent gateway.

Configured credentials stay in the parent relay. Workers receive neither the credential environment nor configured authorization headers. The relay restricts the target and caps bodies at 1 MiB. Worker requests are limited to 64 KiB while reading. Upstream calls have bounded timeouts and a per-request budget. Redirects are not automatically followed.

The model receives contracts, notes, and public diagnostics. It does not receive scenario source, business assertion source, reference code, or private checker code. Provider processes are trusted integrations; send only appropriate contracts and notes to your selected provider.

Keep workspaces private. Recovery records contain request bodies and selected idempotency/concurrency headers. External-project HTTP traces omit bodies by default. Diagnostics and custom assertion messages may contain staging response values. Review output before sharing it. `scripts/package_release.py` redacts machine paths/provider identifiers and excludes private workspaces.

Supported scope: OpenAPI 3.x JSON with local references, bounded REST/JSON workflows, single-host adapters, supplied ID mappings, repeat-safe effects, and declared business assertions. Headers, empty responses, text errors, repeated query parameters, and explicit pending-request replay are supported.

Unsupported guarantees: production gateway uptime, multi-host coordination, distributed transactions, autonomous recovery, streaming, and automatic discovery of business truth. Windows generated execution is unsupported. Staging authentication is configurable; generating a new authentication protocol is outside scope.

An adapter cannot recreate temporal atomicity when the replacement API commits effects separately. A pending workflow may retain a stock hold or charge until explicitly retried. Passing results cover the declared scenarios and final assertions, not every request or intermediate state. Existing client IDs need a supplied mapping when upstream cannot recover them.
