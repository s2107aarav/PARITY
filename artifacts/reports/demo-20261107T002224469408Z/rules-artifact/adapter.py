"""Rules-only operationId baseline; no AI provenance or workflow knowledge."""
import re
from parity.runtime import UpstreamError
ROUTES = [['GET /orders', 'GET /orders'], ['GET /orders/{order_id}', 'GET /orders/{order_ref}']]

def handle(ctx, method, path, body, query, headers):
    with ctx.lock():
        for old, new in ROUTES:
            verb, template = old.split(' ', 1)
            if method != verb:
                continue
            names = re.findall(r'\{([^}]+)\}', template)
            pattern = re.sub(r'\{[^}]+\}', '([^/]+)', template)
            match = re.fullmatch(pattern, path)
            if not match:
                continue
            target_method, target_path = new.split(' ', 1)
            values = list(match.groups())
            for value in values:
                target_path = re.sub(r'\{[^}]+\}', value, target_path, count=1)
            try:
                value = ctx.call(target_method, target_path, body=body, headers=headers, query=query)
                return 200, value
            except UpstreamError as exc:
                return exc.status, exc.data
        return 501, {'error': 'NO_STATIC_OPERATION_MAPPING'}
