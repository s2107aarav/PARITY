from parity.runtime import UpstreamError
import re


def error(status, code):
    return status, {'error': code}


def mapped(exc):
    if exc.status >= 500:
        return error(503, 'PENDING')
    kind = exc.data.get('problem', {}).get('kind', 'INVALID_INPUT')
    table = {
        'PRODUCT_MISSING': (404, 'UNKNOWN_SKU'),
        'BAD_TOTAL': (422, 'AMOUNT_MISMATCH'),
        'DECLINED': (402, 'PAYMENT_DECLINED'),
        'INSUFFICIENT_STOCK': (409, 'OUT_OF_STOCK'),
        'KEY_CONFLICT': (409, 'KEY_REUSED'),
        'MISSING_RESOURCE': (404, 'NOT_FOUND'),
        'INVALID_INPUT': (422, 'INVALID')
    }
    status, code = table.get(kind, (exc.status, kind))
    return error(status, code)


def valid_checkout(body, headers):
    key = headers.get('idempotency-key')
    if not isinstance(key, str) or not 1 <= len(key) <= 120:
        return False
    if not isinstance(body, dict) or set(body) != {'items', 'amount', 'payment_token'}:
        return False
    if type(body['amount']) is not int or body['amount'] <= 0:
        return False
    if not isinstance(body['payment_token'], str):
        return False
    items = body['items']
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        return False
    for item in items:
        if not isinstance(item, dict) or set(item) != {'sku', 'quantity'}:
            return False
        if not isinstance(item['sku'], str):
            return False
        if type(item['quantity']) is not int or not 1 <= item['quantity'] <= 10000:
            return False
    return True


def legacy(ctx, order):
    if order['phase'] not in ('confirmed', 'cancelled'):
        return None
    return {'id': ctx.id_for(order['order_ref']),
            'status': 'paid' if order['phase'] == 'confirmed' else 'cancelled',
            'amount': order['total_minor'],
            'items': [{'sku': line['product_code'], 'quantity': line['units']}
                      for line in order['lines']]}


def current(ctx, ref, status):
    order = ctx.call('GET', '/orders/' + ref)
    result = legacy(ctx, order)
    if result is None:
        return error(503, 'PENDING')
    return status, result


def checkout(ctx, body, headers):
    if not valid_checkout(body, headers):
        return error(422, 'INVALID')
    key = 'checkout:' + ctx.digest(headers['idempotency-key'])
    fingerprint = ctx.digest(body)
    state = ctx.get(key)
    if state is not None and state['stage'] != 'failed':
        if state['fingerprint'] != fingerprint:
            return error(409, 'KEY_REUSED')
    else:
        generation = 1 if state is None else state['generation'] + 1
        state = {'fingerprint': fingerprint, 'generation': generation,
                 'input': body, 'stage': 'draft',
                 'upstream_key': ctx.digest({'key': headers['idempotency-key'], 'generation': generation})}
        ctx.put(key, state)
    if state['stage'] == 'done':
        return current(ctx, state['ref'], 201)
    while True:
        stage = state['stage']
        if stage == 'compensate':
            try:
                ctx.call('POST', '/orders/' + state['ref'] + '/void')
            except UpstreamError:
                return error(503, 'PENDING')
            state['stage'] = 'failed'
            ctx.put(key, state)
            return state['failure'][0], state['failure'][1]
        if stage == 'publish':
            result = legacy(ctx, state['confirmed'])
            if result is None:
                return error(503, 'PENDING')
            state['stage'] = 'done'
            ctx.put(key, state)
            return 201, result
        try:
            if stage == 'draft':
                order = ctx.call('POST', '/orders',
                    body={'lines': [{'product_code': x['sku'], 'units': x['quantity']} for x in body['items']],
                          'total_minor': body['amount']},
                    headers={'idempotency-key': 'draft:' + state['upstream_key']})
                state['ref'] = order['order_ref']
                state['stage'] = 'reserve'
            elif stage == 'reserve':
                ctx.call('POST', '/inventory/reservations', body={'order_ref': state['ref']})
                state['stage'] = 'pay'
            elif stage == 'pay':
                ctx.call('POST', '/payments',
                    body={'order_ref': state['ref'], 'total_minor': body['amount'], 'token': body['payment_token']},
                    headers={'idempotency-key': 'payment:' + state['upstream_key']})
                state['stage'] = 'confirm'
            elif stage == 'confirm':
                order = ctx.call('POST', '/orders/' + state['ref'] + '/confirm')
                if order['phase'] != 'confirmed':
                    return error(503, 'PENDING')
                state['confirmed'] = order
                state['stage'] = 'publish'
            else:
                return error(503, 'PENDING')
            ctx.put(key, state)
        except UpstreamError as exc:
            if exc.status >= 500:
                return error(503, 'PENDING')
            state['failure'] = list(mapped(exc))
            state['stage'] = 'compensate' if 'ref' in state else 'failed'
            ctx.put(key, state)
            if state['stage'] == 'failed':
                return state['failure'][0], state['failure'][1]


def integer(value):
    if not isinstance(value, str) or re.fullmatch(r'[+-]?[0-9]+', value.strip()) is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def quote_segment(value):
    safe = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~'
    return ''.join(chr(n) if chr(n) in safe else '%%%02X' % n for n in value.encode('utf-8'))


def route(ctx, method, path, body, query, headers):
    if method == 'POST' and path == '/checkout':
        return checkout(ctx, body, headers)
    if method == 'GET' and path == '/orders':
        page = integer(query.get('page', '1'))
        size = integer(query.get('page_size', '2'))
        if page is None or size is None or page < 1 or not 1 <= size <= 100:
            return error(422, 'INVALID')
        refs = ctx.mappings()
        if len(refs) > 10000:
            return error(503, 'PENDING')
        refs = sorted(refs, key=lambda ref: ctx.id_for(ref))
        selected = refs[(page - 1) * size:page * size]
        if len(selected) > 80:
            return error(503, 'PENDING')
        orders = []
        for ref in selected:
            status, order = current(ctx, ref, 200)
            if status != 200:
                return status, order
            orders.append(order)
        return 200, {'orders': orders, 'total': len(refs), 'page': page, 'page_size': size}
    match = re.fullmatch(r'/orders/([^/]+)(/cancel)?', path)
    if match and ((method == 'GET' and not match.group(2)) or (method == 'POST' and match.group(2))):
        number = integer(match.group(1))
        if number is None:
            return error(422, 'INVALID')
        ref = ctx.upstream_id(number)
        if ref is None:
            return error(404, 'NOT_FOUND')
        if method == 'GET':
            return current(ctx, ref, 200)
        order = ctx.call('POST', '/orders/' + ref + '/void')
        if order['phase'] != 'cancelled':
            return error(503, 'PENDING')
        return 200, legacy(ctx, order)
    match = re.fullmatch(r'/inventory/([^/]+)', path)
    if method == 'GET' and match:
        stock = ctx.call('GET', '/stock/' + quote_segment(match.group(1)))
        return 200, {'sku': stock['product_code'], 'available': stock['units_available']}
    return error(404, 'NOT_FOUND')


def handle(ctx, method: str, path: str, body: dict | None, query: dict[str, str], headers: dict[str, str]):
    with ctx.lock():
        try:
            return route(ctx, method, path, body, query, headers)
        except UpstreamError as exc:
            return mapped(exc)
