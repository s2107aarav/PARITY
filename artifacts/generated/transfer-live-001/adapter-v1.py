from parity.runtime import UpstreamError
import re

def error(status, code):
    return status, {'error': code}

def translate(exc):
    if exc.status == 503:
        return error(503, 'PENDING')
    kind = exc.data.get('problem', {}).get('kind', '')
    codes = {'INVALID_INPUT': (422, 'INVALID'), 'KEY_CONFLICT': (409, 'KEY_REUSED'), 'MISSING_RESOURCE': (404, 'NOT_FOUND')}
    if kind in codes:
        status, code = codes[kind]
        return error(status, code)
    return error(exc.status, kind or 'UPSTREAM_ERROR')

def create(ctx, body, headers):
    key = headers.get('idempotency-key')
    if not isinstance(key, str) or not 1 <= len(key) <= 120:
        return error(422, 'INVALID')
    if not isinstance(body, dict) or set(body) != {'title', 'body'}:
        return error(422, 'INVALID')
    for field in ('title', 'body'):
        if not isinstance(body[field], str) or not 1 <= len(body[field]) <= 200:
            return error(422, 'INVALID')
    fingerprint = ctx.digest(body)
    storage_key = 'ticket:create:' + key
    record = ctx.get(storage_key)
    if record is None:
        record = {'fingerprint': fingerprint, 'input': dict(body), 'phase': 'draft'}
        ctx.put(storage_key, record)
    elif record['fingerprint'] != fingerprint:
        return error(409, 'KEY_REUSED')
    if record['phase'] == 'done':
        return 201, record['result']
    if record['phase'] == 'failed':
        return record['failure'][0], record['failure'][1]
    try:
        if record['phase'] == 'draft':
            issue = ctx.call('POST', '/issues', body={'summary': record['input']['title']}, headers={'idempotency-key': key})
            record['ref'] = issue['ref']
            record['phase'] = 'comment'
            ctx.put(storage_key, record)
        if record['phase'] == 'comment':
            ctx.call('POST', '/issues/' + record['ref'] + '/comments', body={'text': record['input']['body']})
            record['phase'] = 'publish'
            ctx.put(storage_key, record)
        if record['phase'] == 'publish':
            ctx.call('POST', '/issues/' + record['ref'] + '/publish')
            record['phase'] = 'published'
            ctx.put(storage_key, record)
        result = {'id': ctx.id_for(record['ref']), 'title': record['input']['title'], 'body': record['input']['body'], 'status': 'open'}
        record['result'] = result
        record['phase'] = 'done'
        ctx.put(storage_key, record)
        return 201, result
    except UpstreamError as exc:
        if exc.status == 503 or 'ref' in record:
            return error(503, 'PENDING')
        failure = translate(exc)
        record['failure'] = [failure[0], failure[1]]
        record['phase'] = 'failed'
        ctx.put(storage_key, record)
        return failure

def get_ticket(ctx, token):
    if not re.fullmatch(r'[+-]?[0-9]+', token):
        return error(422, 'INVALID')
    try:
        ticket_id = int(token)
    except ValueError:
        return error(422, 'INVALID')
    ref = ctx.upstream_id(ticket_id)
    if ref is None:
        return error(404, 'NOT_FOUND')
    try:
        issue = ctx.call('GET', '/issues/' + ref)
    except UpstreamError as exc:
        return translate(exc)
    comments = issue.get('comments', [])
    if issue.get('state') != 'open' or len(comments) != 1:
        return error(503, 'PENDING')
    comment = comments[0]
    text = comment.get('text') if isinstance(comment, dict) else comment
    if not isinstance(text, str):
        return error(503, 'PENDING')
    return 200, {'id': ticket_id, 'title': issue['summary'], 'body': text, 'status': 'open'}

def handle(ctx, method: str, path: str, body: dict | None, query: dict[str, str], headers: dict[str, str]):
    with ctx.lock():
        if method == 'POST' and path == '/tickets':
            return create(ctx, body, headers)
        if method == 'GET' and path.startswith('/tickets/') and '/' not in path[len('/tickets/'):]:
            return get_ticket(ctx, path[len('/tickets/'):])
        return error(404, 'NOT_FOUND')
