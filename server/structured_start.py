import math
import json
import re
import graph_values

TYPES = ('string', 'number', 'boolean', 'object', 'array')
RESERVED = {'metadata', 'error', 'selectedOption', 'selectedRoute', '_pauseMetadata',
            '__proto__', 'constructor', 'prototype'}
MAX_FIELDS = 16


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', value) or value in RESERVED:
        raise ValueError('Choose a non-reserved input name beginning with a letter (up to 64 characters).')
    return value


def finite(value):
    try:return math.isfinite(value)
    except (OverflowError, TypeError):return False


def coerce(kind, value):
    if value is None:
        return None
    if kind == 'string':
        if isinstance(value, str):return value
        if type(value) is bool:return 'true' if value else 'false'
        if type(value) in (int, float) and finite(value):return str(value).removesuffix('.0')
        raise ValueError('Text inputs accept text, numbers or booleans.')
    if kind == 'number':
        if type(value) is bool:return int(value)
        if isinstance(value, str):
            text = value.strip()
            try:
                if not text:value = 0
                elif re.fullmatch(r'0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+', text):value = int(text, 0)
                elif re.fullmatch(r'[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?', text):value = float(text)
                else:raise ValueError()
            except (ValueError, OverflowError):raise ValueError('Expected a finite number.') from None
        if type(value) not in (int, float) or not finite(value):raise ValueError('Expected a finite number.')
        return int(value) if value == int(value) else value
    if kind == 'boolean':
        if type(value) is bool:return value
        if isinstance(value, str) and value in ('true', '1'):return True
        if isinstance(value, str) and value in ('false', '0'):return False
        if type(value) in (int, float) and value in (0, 1):return bool(value)

        if isinstance(value, str) and not value.strip():return value
        raise ValueError('Expected true or false.')
    if kind in ('object', 'array'):
        if isinstance(value, str):
            try:value = json.loads(value)
            except ValueError:raise ValueError('Expected valid ' + kind + ' JSON.') from None
        if not isinstance(value, dict if kind == 'object' else list):raise ValueError('Expected a JSON ' + kind + '.')
        return value
    raise ValueError('This input type is unavailable.')


def config(cfg):
    if not isinstance(cfg, dict) or set(cfg) != {'inputFormat', 'runMetadata'} or type(cfg['runMetadata']) is not bool:
        raise ValueError('Choose structured inputs and the run metadata setting.')
    fields = cfg['inputFormat']
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise ValueError('Declare up to sixteen Start fields.')
    names = set(); ids = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) - {'id', 'name', 'type', 'description', 'value'} or not {'id', 'name', 'type', 'description'} <= set(field):
            raise ValueError('Each Start field needs id, name, type and description, plus an optional default value.')
        key = name(field['name']); identifier = field['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier) or identifier in ids or key in names:
            raise ValueError('Use distinct field names and stable field IDs.')
        names.add(key); ids.add(identifier)
        if field['type'] not in TYPES or key == 'files':
            raise ValueError('File inputs are unavailable on this route.')
        if key == 'conversationId' and field['type'] != 'string':raise ValueError('Conversation ID must be text.')
        if not isinstance(field['description'], str) or len(field['description']) > 500:
            raise ValueError('Describe each input in up to 500 characters.')
        if 'value' in field:coerce(field['type'], field['value'])
    graph_values.bound(cfg)
    return cfg


def outputs(cfg):
    result = {'input': 'string', 'conversationId': 'string', 'files': 'array'}
    result.update({f['name']: f['type'] for f in cfg['inputFormat']})
    if cfg['runMetadata']:result['metadata'] = 'object'
    return result


def prepare(cfg, supplied):
    config(cfg)
    if not isinstance(supplied, dict) or len(supplied) > 64:
        raise ValueError('Supply a bounded object of Start inputs.')
    for key in supplied:name(key)
    graph_values.bound(supplied)
    if supplied.get('files') not in (None, []):raise ValueError('File attachments are unavailable on this Start route.')
    result = {'input': '', 'conversationId': '', 'files': []}
    # Additional runtime fields remain plain data; reserved execution fields cannot enter.
    result.update(supplied)
    result['files'] = []
    defaulted = []; coerced = []
    declared = {f['name']: f for f in cfg['inputFormat']}
    for key in ('input', 'conversationId'):
        if key not in declared:result[key] = coerce('string', result[key])
    for field in cfg['inputFormat']:
        key = field['name']
        raw = supplied.get(key)
        if raw is None:
            if 'value' not in field:
                result.pop(key, None)
                continue
            raw = field['value']; defaulted.append(key)
        try:value = coerce(field['type'], raw)
        except ValueError as exc:raise ValueError('Start ' + key + ': ' + str(exc)) from None
        if type(value) is not type(raw) or value != raw:coerced.append(key)
        result[key] = value
    graph_values.bound(result)
    return result, dict(defaultedFields=defaulted, coercedFields=coerced)
