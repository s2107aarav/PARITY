from parity.runtime import UpstreamError
import re

def error(status, code):
    return status, {'error': code}

def upstream_error(exc):
    if exc.status == 503:
        return error(503, 'PENDING')
    kind = exc.data.get('problem', {}).get('kind')
    mappings = {'KEY_CONFLICT': (409, 'KEY_REUSED'), 'INVALID_INPUT': (422, 'INVALID'), 'MISSING_RESOURCE': (404, 'NOT_FOUND')}
    if kind in mappings:
        status, code = mappings[kind]
        return error(status, code)
    return exc.status, exc.data

def create(ctx, body, headers):
    key = headers.get('idempotency-key')
    if not isinstance(key, str) or not 1 <= len(key) <= 120:
        return error(422, 'INVALID')
    if not isinstance(body, dict) or set(body) != {'title', 'body'}:
        return error(422, 'INVALID')
    for name in ('title', 'body'):
        if not isinstance(body[name], str) or not 1 <= len(body[name]) <= 200:
            return error(422, 'INVALID')
    storage = 'moderated:request:' + key
    fingerprint = ctx.digest(body)
    state = ctx.get(storage)
    if state is None:
        state = {'fingerprint': fingerprint, 'input': dict(body), 'stage': 'create'}
        ctx.put(storage, state)
    elif state['fingerprint'] != fingerprint:
        return error(409, 'KEY_REUSED')
    if 'result' in state:
        return 201, state['result']
    if 'ref' not in state:
        issue = ctx.call('POST', '/issues', body={'summary': body['title']}, headers={'idempotency-key': key})
        state['ref'] = issue['ref']
        state['stage'] = 'comment'
        ctx.put(storage, state)
    ref = state['ref']
    base = '/issues/' + ref
    if not state.get('commented'):
        ctx.call('POST', base + '/comments', body={'text': body['body']})
        state['commented'] = True
        state['stage'] = 'approve'
        ctx.put(storage, state)
    if 'receipt' not in state:
        approval = ctx.call('POST', base + '/approve')
        state['receipt'] = approval['receipt']
        state['stage'] = 'publish'
        ctx.put(storage, state)
    if not state.get('published'):
        issue = ctx.call('POST', base + '/publish', body={'receipt': state['receipt']})
        if issue.get('state') != 'open':
            return error(503, 'PENDING')
        state['published'] = True
        state['stage'] = 'identify'
        ctx.put(storage, state)
    ctx.put('moderated:issue:' + ref, {'title': body['title'], 'body': body['body']})
    result = {'id': ctx.id_for(ref), 'title': body['title'], 'body': body['body'], 'status': 'open'}
    state['result'] = result
    state['stage'] = 'done'
    ctx.put(storage, state)
    return 201, result

def retrieve(ctx, text):
    if not re.fullmatch(r'[+-]?[0-9]+', text) or len(text) > 100:
        return error(422, 'INVALID')
    ticket_id = int(text)
    ref = ctx.upstream_id(ticket_id)
    if ref is None:
        return error(404, 'NOT_FOUND')
    original = ctx.get('moderated:issue:' + ref)
    if original is None:
        return error(404, 'NOT_FOUND')
    issue = ctx.call('GET', '/issues/' + ref)
    if issue.get('state') != 'open':
        return error(404, 'NOT_FOUND')
    return 200, {'id': ticket_id, 'title': issue['summary'], 'body': original['body'], 'status': 'open'}

def handle(ctx, method: str, path: str, body: dict | None, query: dict[str, str], headers: dict[str, str]):
    with ctx.lock():
        try:
            if method == 'POST' and path == '/tickets':
                return create(ctx, body, headers)
            if method == 'GET' and path.startswith('/tickets/') and '/' not in path[len('/tickets/'):]:
                return retrieve(ctx, path[len('/tickets/'):])
            return error(404, 'NOT_FOUND')
        except UpstreamError as exc:
            return upstream_error(exc)
