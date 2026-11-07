"""A deterministic endpoint mapper, deliberately without semantic saga synthesis."""

import json
from pathlib import Path

from parity.contracts import operations
from parity.sandbox import digest


def rules_adapter(old, new, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    right = operations(new)
    routes = []
    for old_name, op in operations(old).items():
        target = next(
            (
                name
                for name, candidate in right.items()
                if op.get("operationId") and candidate.get("operationId") == op["operationId"]
            ),
            None,
        )
        if target:
            routes.append([old_name, target])
    source = (
        '''"""Rules-only operationId baseline; no AI provenance or workflow knowledge."""
import re
from parity.runtime import UpstreamError
ROUTES = '''
        + repr(routes)
        + r"""

def handle(ctx, method, path, body, query, headers):
    with ctx.lock():
        for old, new in ROUTES:
            verb, template = old.split(' ', 1)
            if method != verb:
                continue
            pattern = '([^/]+)'.join(re.escape(part) for part in re.split(r'\{[^}]+\}', template))
            match = re.fullmatch(pattern, path)
            if not match:
                continue
            target_method, target_path = new.split(' ', 1)
            values = list(match.groups())
            for value in values:
                target_path = re.sub(r'\{[^}]+\}', lambda _: value, target_path, count=1)
            try:
                value = ctx.call(target_method, target_path, body=body, headers=headers, query=query)
                return 200, value
            except UpstreamError as exc:
                return exc.status, exc.data
        return 501, {'error': 'NO_STATIC_OPERATION_MAPPING'}
"""
    )
    (output / "adapter.py").write_text(source)
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "provenance": "rules-only-baseline",
                "adapter_sha256": digest(output / "adapter.py"),
                "mappings": routes,
                "limitations": [
                    "Matches operationId and path parameters only; cannot synthesize one-to-many semantics."
                ],
            },
            indent=2,
        )
    )
    return output / "adapter.py"
