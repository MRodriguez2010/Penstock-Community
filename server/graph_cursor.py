"""Schedule selected edges of a validated DAG on the existing durable, serial run cursor."""
from collections import deque


# Node-addressed outputs and named workflow variables are run-scoped. Implicit
# predecessor state and its verification belong to one path through the graph.
PATH_FIELDS = ('previousNode', 'previousSucceeded', 'weatherNode', 'artifact', 'verified',
               'verificationFailed', 'resultVerification')
TERMINAL_FIELDS = ('result', 'response', 'responseSurface')


def topological_order(graph):
    incoming = {node['id']: 0 for node in graph['nodes']}
    outgoing = {key: [] for key in incoming}
    for edge in graph['edges']:
        outgoing[edge['source']].append(edge['target'])
        incoming[edge['target']] += 1
    ready = deque(key for key, count in incoming.items() if not count)
    ordered = []
    while ready:
        key = ready.popleft()
        ordered.append(key)
        for target in outgoing[key]:
            incoming[target] -= 1
            if not incoming[target]:
                ready.append(target)
    return ordered


def advance(graph, ctx, targets, terminal_state, target_success=None):
    """Return (next cursor, terminal run state). Called in the step result transaction."""
    if len(targets) <= 1 and 'fanout' not in ctx:
        if targets and target_success is not None:
            ctx['previousSucceeded'] = target_success[targets[0]]
        return (targets[0], None) if targets else (None, terminal_state)

    if 'fanout' not in ctx:
        ctx['fanout'] = {'order': topological_order(graph), 'pending': {}}
    routing = ctx['fanout']
    path = {field: ctx[field] for field in PATH_FIELDS if field in ctx}
    if ctx.get('verificationFailed') or ctx.get('resultVerification', {}).get('verification') == 'failed':
        routing['verificationFailed'] = True
    for target in targets:
        # A join receives implicit predecessor state from the first selected
        # incoming edge to arrive. All selected predecessor outputs are addressable.
        target_path = path if target_success is None else {**path, 'previousSucceeded': target_success[target]}
        routing['pending'].setdefault(target, target_path)

    if not targets:
        if terminal_state == 'failed':
            routing['failed'] = True
        else:
            # One public result per run: prefer a Response, then a result-bearing
            # terminal, then an empty terminal. Ties retain the first reached.
            rank = 2 if 'response' in ctx else 1 if 'result' in ctx else 0
            if rank > routing.get('terminalRank', -1):
                routing['terminalRank'] = rank
                routing['terminal'] = {**path, **{k: ctx[k] for k in TERMINAL_FIELDS if k in ctx}}

    for field in PATH_FIELDS + TERMINAL_FIELDS:
        ctx.pop(field, None)
    if routing['pending']:
        # Even a direct edge to a join waits for a longer selected path. A node
        # earlier in this order cannot be activated by a later node.
        target = next(key for key in routing['order'] if key in routing['pending'])
        ctx.update(routing['pending'].pop(target))
        return target, None

    ctx.pop('fanout')
    ctx.update(routing.get('terminal', {}))
    if routing.get('verificationFailed'):
        ctx['verificationFailed'] = True
    return None, 'failed' if routing.get('failed') else 'finished'
