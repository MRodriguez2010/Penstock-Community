"""Live project-table operations with atomic mutation/receipt and revision-bound pages."""
import base64
import datetime
import hashlib
import json
import time
import uuid
from decimal import Decimal

import graph_values
import project_resources as res
import typed_tables as tables
from store import row

KIND = 'table'
MAX_ROWS = 5000
MAX_BATCH = 100
REQUIRED = {
    'query_rows': set(), 'get_schema': set(), 'insert_row': {'data'},
    'upsert_row': {'data'}, 'batch_insert_rows': {'rows'},
    'get_row': {'rowId'}, 'update_row': {'rowId', 'data'}, 'delete_row': {'rowId'},
    'update_rows_by_filter': {'filter', 'data'}, 'delete_rows_by_filter': {'filter'},
}
OPTIONAL = {
    'query_rows': {'filter', 'order', 'limit', 'cursor', 'columns'},
    'upsert_row': {'conflictColumn'}, 'update_rows_by_filter': {'limit'},
    'delete_rows_by_filter': {'limit'},
}
SOURCES = {'tableId': 'string', 'rowId': 'string', 'data': 'object', 'rows': 'array',
           'filter': 'object', 'order': 'array', 'limit': 'number', 'cursor': 'string',
           'columns': 'array', 'conflictColumn': 'string'}
OUTPUTS = {
    'query_rows': {'rows': 'array', 'rowCount': 'number', 'totalCount': None, 'nextCursor': None},
    'get_schema': {'name': 'string', 'columns': 'array', 'columnCount': 'number', 'rowCount': 'number', 'maxRows': 'number'},
    'get_row': {'row': 'object'}, 'insert_row': {'row': 'object'}, 'update_row': {'row': 'object'},
    'upsert_row': {'row': 'object', 'operation': 'string'},
    'batch_insert_rows': {'rows': 'array', 'insertedCount': 'number'},
    'update_rows_by_filter': {'updatedCount': 'number', 'updatedRowIds': 'array'},
    'delete_row': {'deletedCount': 'number'},
    'delete_rows_by_filter': {'deletedCount': 'number', 'deletedRowIds': 'array'},
}
VALUELESS = {'isNull', 'isNotNull', 'isEmpty', 'isNotEmpty'}
OPS = VALUELESS | {'eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'nin', 'like', 'ilike',
                   'nlike', 'nilike', 'contains', 'ncontains', 'startsWith', 'endsWith'}


def outputs(op):
    return {'success': 'boolean', 'message': 'string', **OUTPUTS[op]}


def active(op):
    if not isinstance(op, str) or op not in REQUIRED:
        raise ValueError('Choose a supported Table operation.')
    return REQUIRED[op] | OPTIONAL.get(op, set()) | {'tableId'}


def predicate(value, schema=None, depth=0, budget=None):
    budget = [0] if budget is None else budget
    budget[0] += 1
    if depth > 8 or budget[0] > 64 or not isinstance(value, dict):
        raise ValueError('Use a predicate with at most 64 conditions and eight nested groups.')
    groups = set(value) & {'all', 'any'}
    if groups:
        if len(groups) != 1 or set(value) != groups:
            raise ValueError('Use one all/any group or one field condition, never both.')
        members = value[next(iter(groups))]
        if not isinstance(members, list) or not members:
            raise ValueError('A filter group needs at least one condition.')
        for member in members:
            predicate(member, schema, depth + 1, budget)
        return
    op = value.get('op')
    if not isinstance(op, str) or op not in OPS or set(value) != ({'field', 'op'} if op in VALUELESS else {'field', 'op', 'value'}):
        raise ValueError('Choose a supported predicate operator and its required value.')
    field = value['field']
    if not isinstance(field, str) or not field or len(field) > 128:
        raise ValueError('Choose a real column name or ID for the filter.')
    col = resolve_column(schema, field) if schema is not None else None
    if col and col['type'] == 'select' and op not in VALUELESS | {'eq', 'ne', 'in', 'nin'}:
        raise ValueError('Select columns support equality, membership and empty/null checks.')
    if op not in VALUELESS:
        v = value['value']
        if op in ('in', 'nin'):
            if not isinstance(v, list) or not 1 <= len(v) <= 1000 or any(isinstance(x, (list, dict)) for x in v):
                raise ValueError('Membership requires 1–1000 scalar values.')
        elif isinstance(v, (dict, list)):
            raise ValueError('This predicate needs a scalar value.')


def validate_value(name, value):
    graph_values.bound(value)
    expected = SOURCES[name]
    valid = {'string': isinstance(value, str), 'number': type(value) is int,
             'object': isinstance(value, dict), 'array': isinstance(value, list)}[expected]
    if not valid:
        raise ValueError('Table ' + name + ' requires ' + expected + '.')
    if name in ('tableId', 'rowId'):
        res.ident(value)
    if name in ('data', 'rows') and not value:
        raise ValueError('Provide nonempty row data.')
    if name == 'rows' and (len(value) > MAX_BATCH or any(not isinstance(x, dict) or not x for x in value)):
        raise ValueError('Insert 1–100 nonempty row objects per batch.')
    if name == 'filter':
        predicate(value)
    if name == 'order':
        if len(value) > 24 or any(not isinstance(v, dict) or set(v) != {'field', 'direction'} or
                                 not isinstance(v['field'], str) or v['direction'] not in ('asc', 'desc') for v in value):
            raise ValueError('Sort by up to 24 fields with asc/desc directions.')
        if len({v['field'] for v in value}) != len(value):
            raise ValueError('Sort fields must be distinct.')
    if name == 'columns' and (len(value) > 24 or any(not isinstance(v, str) or not v for v in value)):
        raise ValueError('Select up to 24 output columns by name or ID.')
    if name == 'limit' and not 1 <= value <= MAX_ROWS:
        raise ValueError('Set a row limit from 1 to 5000.')
    if name == 'conflictColumn' and not value:
        raise ValueError('Select a unique conflict column.')
    if name == 'cursor' and len(value) > 2048:
        raise ValueError('Use a returned page cursor.')
    return value


def config(cfg):
    if not isinstance(cfg, dict):
        raise ValueError('Choose a Table operation.')
    op = cfg.get('operation')
    allowed = active(op)
    if set(cfg) - allowed - {'operation'} or not REQUIRED[op] | {'tableId'} <= set(cfg):
        raise ValueError('Provide only the fields for the selected Table operation.')
    for name in allowed & set(cfg):
        source = cfg[name]
        if not isinstance(source, dict):
            raise ValueError('Choose a literal or bound Table input.')
        if set(source) == {'ref'}:
            graph_values.check_reference(source['ref'])
        elif set(source) == {'literal'}:
            validate_value(name, source['literal'])
        else:
            raise ValueError('Choose a literal or bound Table input.')
    return cfg


def resolve_column(schema, key, missing=False):
    found = [col for col in schema if key in (col['id'], col['name'])]
    if len(found) == 1:
        return found[0]
    if missing and not found:
        return None
    raise ValueError('Unknown or ambiguous Table column: ' + str(key))


def typed(col, raw):
    if raw == '':
        return None
    if col['type'] == 'number':
        number = Decimal(raw)
        return int(number) if number == number.to_integral_value() else float(number)
    if col['type'] == 'boolean':
        return raw == 'true'
    return raw


def storage(col, value):
    if value is None:
        return ''
    if col['type'] == 'number':
        if type(value) not in (int, float):
            raise ValueError('Numeric columns require JSON numbers.')
        value = str(value)
    elif col['type'] == 'boolean':
        if type(value) is not bool:
            raise ValueError('Boolean columns require JSON true/false.')
        value = str(value).lower()
    elif not isinstance(value, str):
        raise ValueError('Text/select/date columns require JSON text.')
    return tables.checked(col, value)


def row_data(schema, supplied):
    result = {}
    for key, value in supplied.items():
        col = resolve_column(schema, key)
        if col['id'] in result:
            raise ValueError('A row must not name the same column twice.')
        result[col['id']] = storage(col, value)
    return result


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def read_rows(c, meta):
    if meta['rowCount'] > MAX_ROWS:
        raise ValueError('This route supports project tables with up to 5000 rows.')
    found = []; scanned = 0
    for index, rec in enumerate(c.execute('''SELECT r.id,v.version,r.values_json,r.created,t.updated
        FROM project_records r JOIN record_versions v ON v.record=r.id
        LEFT JOIN graph_table_record_times t ON t.record=r.id WHERE r.table_id=? ORDER BY r.rowid''', (meta['id'],))):
        scanned += len(rec['values_json'].encode())
        if scanned > 16 * 1024 * 1024:
            raise ValueError('This table exceeds the installed 16 MiB scan budget.')
        raw = json.loads(rec['values_json'])
        found.append(dict(id=rec['id'], version=rec['version'], raw=raw,
                          data={col['name']: typed(col, val) for col, val in zip(meta['schema'], raw)},
                          createdAt=iso(rec['created']), updatedAt=iso(rec['updated'] or rec['created']), position=index))
    return found


def public(rec, columns=None):
    data = rec['data'] if columns is None else {k: v for k, v in rec['data'].items() if k in columns}
    return {**{k: rec[k] for k in ('id', 'version', 'position', 'createdAt', 'updatedAt')}, 'data': data}


def value_for(rec, col):
    return rec[col['name']] if col.get('system') else rec['data'][col['name']]


def query_schema(meta):
    # Ambiguous reserved-name columns are rejected rather than reading the wrong value.
    return meta['schema'] + [dict(id=k, name=k, type='text', system=True) for k in ('id', 'createdAt', 'updatedAt')]


def matches(rec, f, schema):
    if 'all' in f:
        return all(matches(rec, x, schema) for x in f['all'])
    if 'any' in f:
        return any(matches(rec, x, schema) for x in f['any'])
    v = value_for(rec, resolve_column(schema, f['field']))
    op = f['op']; wanted = f.get('value')
    empty = v is None or v == ''
    if op in VALUELESS:
        return (v is None if op == 'isNull' else v is not None if op == 'isNotNull' else empty if op == 'isEmpty' else not empty)
    def eq(a, b):
        return type(a) is type(b) and a == b or type(a) in (int, float) and type(b) in (int, float) and a == b
    if op in ('eq', 'ne'):
        return eq(v, wanted) if op == 'eq' else not eq(v, wanted)
    if op in ('in', 'nin'):
        present = any(eq(v, x) for x in wanted)
        return present if op == 'in' else not present
    if op in ('gt', 'gte', 'lt', 'lte'):
        if v is None or wanted is None or type(v) is bool or type(wanted) is bool:
            return False
        if not (isinstance(v, str) and isinstance(wanted, str) or type(v) in (int, float) and type(wanted) in (int, float)):
            raise ValueError('Comparison value must match the column type.')
        return {'gt': v > wanted, 'gte': v >= wanted, 'lt': v < wanted, 'lte': v <= wanted}[op]
    text = '' if v is None else str(v)
    if not isinstance(wanted, str):
        raise ValueError('Text comparisons require a string value.')
    if op in ('contains', 'ncontains', 'startsWith', 'endsWith'):
        a, b = text.casefold(), wanted.casefold()
        return b in a if op == 'contains' else b not in a if op == 'ncontains' else a.startswith(b) if op == 'startsWith' else a.endswith(b)
    # A wildcard matcher with O(text * pattern) worst case and no regex backtracking.
    a, b = (text.casefold(), wanted.casefold()) if op in ('ilike', 'nilike') else (text, wanted)
    parts = b.split('*'); offset = 0; good = True
    for i, part in enumerate(parts):
        pos = len(a)-len(part) if i == len(parts)-1 and len(parts)>1 and not b.endswith('*') and a.endswith(part) else a.find(part, offset)
        if pos < offset or i == 0 and pos != 0:
            good = False; break
        offset = pos + len(part)
    good = good and (b.endswith('*') or offset == len(a))
    return not good if op in ('nlike', 'nilike') else good


def perform(c, job, inputs):
    op = inputs['operation']; tid = inputs['tableId']; project = job['project']; chat = job['chat']
    meta = tables.editable(c, project, tid); schema = meta['schema']; qschema = query_schema(meta)
    before = meta['revision']; records = read_rows(c, meta) if op != 'get_schema' else []
    for entry in inputs.get('order', []):
        resolve_column(qschema, entry['field'])
    if 'filter' in inputs:
        predicate(inputs['filter'], qschema)
    selected = [r for r in records if 'filter' not in inputs or matches(r, inputs['filter'], qschema)]
    out = dict(success=True, message='Table operation completed.')
    def insert(data):
        if tables.editable(c, project, tid)['rowCount'] >= MAX_ROWS:
            raise ValueError('The table row limit is 5000.')
        return tables.append(c, project, chat, {'id': tid, 'values': row_data(schema, data)})['id']
    def update(rec, data):
        changes = row_data(schema, data)
        return tables.replace_record(c, project, chat, tid, rec['id'], rec['version'], changes)
    def retrieve(rid):
        return public(next(r for r in read_rows(c, tables.editable(c, project, tid)) if r['id'] == rid))
    if op == 'get_schema':
        out.update(name=meta['name'], columns=schema, columnCount=len(schema), rowCount=meta['rowCount'], maxRows=MAX_ROWS)
    elif op == 'query_rows':
        for order in reversed(inputs.get('order', [])):
            col = resolve_column(qschema, order['field'])
            present = [r for r in selected if value_for(r, col) is not None]
            blank = [r for r in selected if value_for(r, col) is None]
            selected = sorted(present, key=lambda r: value_for(r, col), reverse=order['direction'] == 'desc') + blank
        fingerprint = hashlib.sha256(json.dumps({k: v for k, v in inputs.items() if k not in ('cursor', 'limit')}, sort_keys=True).encode()).hexdigest()
        offset = 0; cursor = inputs.get('cursor')
        if cursor and cursor not in ('null', 'undefined'):
            try:
                token = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if set(token) != {'query', 'revision', 'offset'} or token['query'] != fingerprint or token['revision'] != before or type(token['offset']) is not int or not 0 <= token['offset'] <= len(selected):
                    raise ValueError()
                offset = token['offset']
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise ValueError('The page cursor is invalid or the table changed. Query again from the first page.') from None
        cols = None
        if inputs.get('columns'):
            cols = {col['name'] for k in inputs['columns'] if (col := resolve_column(schema, k, True))}
        stop = min(len(selected), offset + inputs.get('limit', len(selected)))
        result = []; size = 0
        for index, rec in enumerate(selected[offset:stop], offset):
            item = public(rec, cols)
            item_size = len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode()) + 2
            if 'limit' in inputs and result and size + item_size > graph_values.MAX_BYTES - 2048:
                stop = index
                break
            result.append(item); size += item_size
        next_cursor = base64.urlsafe_b64encode(json.dumps(dict(query=fingerprint, revision=before, offset=stop)).encode()).decode() if stop < len(selected) else None
        out.update(rows=result, rowCount=len(result), totalCount=len(selected) if offset == 0 else None, nextCursor=next_cursor)
    elif op in ('insert_row', 'batch_insert_rows'):
        ids = [insert(v) for v in (inputs['rows'] if op == 'batch_insert_rows' else [inputs['data']])]
        if op == 'insert_row':
            out['row'] = retrieve(ids[0])
        else:
            out.update(rows=[retrieve(rid) for rid in ids], insertedCount=len(ids))
    elif op == 'upsert_row':
        unique = [col for col in schema if col['unique']]
        col = resolve_column(schema, inputs['conflictColumn']) if 'conflictColumn' in inputs else unique[0] if len(unique) == 1 else None
        if not col or not col['unique']:
            raise ValueError('Choose one unique conflict column for upsert.')
        changes = row_data(schema, inputs['data'])
        if not changes.get(col['id']):
            raise ValueError('Upsert requires a nonempty value in its conflict column.')
        pos = schema.index(col)
        match = next((r for r in records if r['raw'][pos] == changes[col['id']]), None)
        if match:
            update(match, inputs['data']); rid = match['id']
        else:
            rid = insert(inputs['data'])
        out.update(row=retrieve(rid), operation='update' if match else 'insert')
    else:
        if 'rowId' in inputs:
            selected = [r for r in records if r['id'] == inputs['rowId']]
            if not selected:
                raise ValueError('Row unavailable in this table.')
        selected = selected[:inputs.get('limit', len(selected))]
        if op == 'get_row':
            out['row'] = public(selected[0])
        elif op in ('update_row', 'update_rows_by_filter'):
            for rec in selected:
                update(rec, inputs['data'])
            if op == 'update_row':
                out['row'] = retrieve(selected[0]['id'])
            else:
                out.update(updatedCount=len(selected), updatedRowIds=[r['id'] for r in selected])
        else:
            for rec in selected:
                tables.delete_row(c, project, chat, dict(id=tid, row=rec['id'], expectedVersion=rec['version'], confirmed=True))
            out['deletedCount'] = len(selected)
            if op == 'delete_rows_by_filter':
                out['deletedRowIds'] = [r['id'] for r in selected]
    graph_values.bound(out)
    return dict(status='done', port='success', detail=out['message'], values=out,
                tableRevisionBefore=before, tableRevisionAfter=tables.editable(c, project, tid)['revision'])
