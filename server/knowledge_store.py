"""Versioned local knowledge chunks. Original document versions and run snapshots stay readable."""
import datetime
import json
import uuid

import project_resources as resources
from store import now, row, rows, uid

SLOTS = {**{f'tag{i}': 'text' for i in range(1, 8)}, **{f'number{i}': 'number' for i in range(1, 6)},
         **{f'date{i}': 'date' for i in range(1, 3)}, **{f'boolean{i}': 'boolean' for i in range(1, 4)}}


def stamp(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def tags(value):
    if not isinstance(value, dict) or set(value) - SLOTS.keys():
        raise ValueError('Use tag1–7, number1–5, date1–2 or boolean1–3 for document tags.')
    for key, v in value.items():
        kind = SLOTS[key]
        valid = isinstance(v, str) if kind in ('text', 'date') else type(v) in (int, float) if kind == 'number' else type(v) is bool
        if not valid or isinstance(v, str) and len(v) > 1000:
            raise ValueError(key + ' requires a ' + kind + ' value (text up to 1000 characters).')
        if kind == 'date':
            try:
                if len(v) == 10: datetime.date.fromisoformat(v)
                elif datetime.datetime.fromisoformat(v.replace('Z', '+00:00')).tzinfo is None: raise ValueError()
            except ValueError:
                raise ValueError(key + ' requires an ISO date or timestamp with timezone.') from None
    return value


def source(c, project, document):
    found = row(c, resources.DOCS + ' AND k.id=?', project, document)
    if not found: raise resources.Missing('Document unavailable in this project knowledge base.')
    return found


def state(c, document, original=None):
    found = row(c, 'SELECT * FROM knowledge_state WHERE document=?', document)
    if found:
        found['tags'] = json.loads(found['tags'])
        return found
    if original is None: raise ValueError('Knowledge metadata unavailable.')
    return dict(document=document, revision=0, enabled=1, deleted=0, tags={}, created=original['updated'], updated=original['updated'])


def chunks(c, document):
    return rows(c, 'SELECT * FROM knowledge_chunks WHERE document=? AND deleted=0 ORDER BY chunk_index,id', document)


def checkpoint(c, document):
    meta = state(c, document)
    original = row(c, 'SELECT version FROM knowledge_docs WHERE id=?', document)
    snapshot = dict(metadata=meta, documentVersion=original['version'], chunks=chunks(c, document))
    c.execute('INSERT INTO knowledge_history VALUES(?,?,?)', (document, meta['revision'], json.dumps(snapshot, ensure_ascii=False)))


def touch(c, document, **changes):
    # Only named internal columns are mutable; user input never becomes SQL.
    if set(changes) - {'tags', 'deleted', 'enabled'}: raise ValueError('Invalid knowledge state field.')
    if 'tags' in changes: changes['tags'] = json.dumps(changes['tags'], ensure_ascii=False)
    assignments = ','.join(k + '=?' for k in changes)
    c.execute('UPDATE knowledge_state SET revision=revision+1,updated=?' + (',' + assignments if assignments else '') + ' WHERE document=?',
              (now(), *changes.values(), document))


def ingest(c, project, document):
    """Every explicit document save replaces chunks; old bytes remain in document/history versions."""
    original = source(c, project, document)
    at = now()
    c.execute('INSERT OR IGNORE INTO knowledge_state VALUES(?,0,1,0,?,?,?)', (document, '{}', original['updated'], at))
    c.execute('UPDATE knowledge_chunks SET deleted=1 WHERE document=?', (document,))
    body = original['text']
    for index, offset in enumerate(range(0, len(body), 4000)):
        c.execute('INSERT INTO knowledge_chunks VALUES(?,?,?,?,1,0,?,?)', (uid(), document, index, body[offset:offset + 4000], at, at))
    touch(c, document)
    checkpoint(c, document)


def ensure(c, project, document):
    original = source(c, project, document)
    if not row(c, 'SELECT 1 FROM knowledge_state WHERE document=?', document): ingest(c, project, document)
    return original


def chunk_public(rec):
    return dict(id=rec['id'], chunkIndex=rec['chunk_index'], content=rec['content'], contentLength=len(rec['content']),
                tokenCount=None, enabled=bool(rec['enabled']), createdAt=stamp(rec['created']), updatedAt=stamp(rec['updated']))


def document_public(c, project, document, full=False):
    original = source(c, project, document)
    meta = state(c, document, original)
    count = row(c, 'SELECT count(*) AS n FROM knowledge_chunks WHERE document=? AND deleted=0', document)['n'] if meta['revision'] else (len(original['text']) + 3999) // 4000
    result = dict(id=document, filename=original['title'], fileSize=len(original['text'].encode()), mimeType='text/plain',
                  enabled=bool(meta['enabled']), processingStatus='completed', chunkCount=count, tokenCount=None,
                  uploadedAt=stamp(meta['created']), updatedAt=stamp(meta['updated']), connectorId=None, connectorType=None,
                  sourceUrl=None, version=original['version'], revision=meta['revision'], processingMode='local-fixed-characters', embeddingStatus='not-configured')
    if full: result.update(characterCount=len(original['text']), processingError=None, externalId=None, tags=meta['tags'])
    return result


def set_tags(c, project, document, value):
    tags(value)
    for slot in value:
        tid = str(uuid.uuid5(uuid.UUID(project), 'knowledge-tag:' + slot))
        c.execute('INSERT OR IGNORE INTO knowledge_tag_definitions VALUES(?,?,?,?,?)', (tid, project, slot, SLOTS[slot], now()))
    # The caller advances one revision for its entire operation.
    c.execute('UPDATE knowledge_state SET tags=? WHERE document=?', (json.dumps(value, ensure_ascii=False), document))


def definitions(c, project):
    return [dict(id=t['id'], tagSlot=t['slot'], displayName=t['slot'], fieldType=t['type'], createdAt=stamp(t['created']), updatedAt=stamp(t['created']))
            for t in rows(c, 'SELECT * FROM knowledge_tag_definitions WHERE project=? ORDER BY slot', project)]
