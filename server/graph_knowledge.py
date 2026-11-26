"""Project knowledge operations with mutation and first-result receipt in one SQLite transaction."""
import hashlib
import json
import time
import uuid

import graph_values
import knowledge_store as knowledge
import project_resources as resources
from store import now, row, rows, uid

KIND = 'knowledge'
REQUIRED = {'list_documents': set(), 'get_document': {'documentId'}, 'create_document': {'name', 'content'},
            'upsert_document': {'name', 'content'}, 'delete_document': {'documentId'}, 'list_chunks': {'documentId'},
            'upload_chunk': {'documentId', 'content'}, 'update_chunk': {'documentId', 'chunkId'},
            'delete_chunk': {'documentId', 'chunkId'}, 'list_tags': set()}
OPTIONAL = {'list_documents': {'search', 'enabledFilter', 'limit', 'offset'},
            'create_document': {'documentTags'}, 'upsert_document': {'documentId', 'documentTags', 'expectedRevision'},
            'delete_document': {'expectedRevision'}, 'list_chunks': {'search', 'enabledFilter', 'limit', 'offset'},
            'upload_chunk': {'expectedRevision'}, 'update_chunk': {'content', 'enabled', 'expectedRevision'}, 'delete_chunk': {'expectedRevision'}}
TYPES = {'knowledgeBaseId': 'string', 'documentId': 'string', 'chunkId': 'string', 'name': 'string', 'content': 'string',
         'documentTags': 'object', 'search': 'string', 'enabledFilter': 'string', 'limit': 'number', 'offset': 'number',
         'enabled': 'boolean', 'expectedRevision': 'number'}
MUTATIONS = {'create_document', 'upsert_document', 'delete_document', 'upload_chunk', 'update_chunk', 'delete_chunk'}
def active(op):
    if not isinstance(op, str) or op not in REQUIRED: raise ValueError('Choose an available local Knowledge operation. Semantic search and connectors are not configured.')
    return REQUIRED[op] | OPTIONAL.get(op, set()) | {'knowledgeBaseId'}


def validate(name, value):
    graph_values.bound(value)
    typ = TYPES[name]
    ok = isinstance(value, str) if typ == 'string' else type(value) is int if typ == 'number' else type(value) is bool if typ == 'boolean' else isinstance(value, dict)
    if not ok: raise ValueError('Knowledge ' + name + ' requires ' + typ + '.')
    if name in ('knowledgeBaseId', 'documentId', 'chunkId'): value = resources.ident(value)
    if name == 'name': resources.name(value, 'document name')
    if name == 'content': resources.bounded(value, 'document/chunk content', 32000)
    if name == 'search': resources.bounded(value, 'search', 180, True)
    if name == 'enabledFilter' and value not in ('all', 'enabled', 'disabled'): raise ValueError('Choose all, enabled or disabled.')
    if name == 'limit' and not 1 <= value <= 100: raise ValueError('Knowledge page limit must be 1–100.')
    if name == 'offset' and not 0 <= value <= 1000000: raise ValueError('Knowledge offset must be 0–1000000.')
    if name == 'expectedRevision' and value < 0: raise ValueError('Use the current Knowledge document revision.')
    if name == 'documentTags': knowledge.tags(value)
    return value


def config(cfg):
    if not isinstance(cfg, dict): raise ValueError('Choose a Knowledge operation.')
    op = cfg.get('operation'); allowed = active(op)
    if set(cfg) - allowed - {'operation'} or not REQUIRED[op] | {'knowledgeBaseId'} <= cfg.keys(): raise ValueError('Provide only the selected Knowledge operation fields.')
    if op == 'update_chunk' and not {'content', 'enabled'} & cfg.keys(): raise ValueError('Set chunk content or enabled status.')
    for key, value in cfg.items():
        if key == 'operation': continue
        if not isinstance(value, dict): raise ValueError('Choose a literal or connected Knowledge input.')
        if set(value) == {'ref'}: graph_values.check_reference(value['ref'])
        elif set(value) == {'literal'}: validate(key, value['literal'])
        else: raise ValueError('Choose a literal or connected Knowledge input.')
    graph_values.bound(cfg)
    return cfg


def outputs(op):
    common = {'knowledgeBaseId': 'string'}
    fields = {
        'list_documents': {'documents': 'array', 'totalDocuments': 'number', 'limit': 'number', 'offset': 'number'},
        'get_document': {'id': 'string', 'filename': 'string', 'fileSize': 'number', 'mimeType': 'string', 'enabled': 'boolean',
                         'processingStatus': 'string', 'chunkCount': 'number', 'tokenCount': None, 'characterCount': 'number', 'tags': 'object', 'version': 'number', 'revision': 'number',
                         'uploadedAt': 'string', 'updatedAt': 'string', 'connectorId': None, 'connectorType': None, 'sourceUrl': None,
                         'processingMode': 'string', 'embeddingStatus': 'string', 'processingError': None, 'externalId': None},
        'create_document': {'data': 'object', 'documentId': 'string', 'message': 'string', 'revision': 'number'},
        'upsert_document': {'data': 'object', 'documentId': 'string', 'message': 'string', 'revision': 'number'},
        'delete_document': {'documentId': 'string', 'message': 'string', 'revision': 'number'},
        'list_chunks': {'documentId': 'string', 'chunks': 'array', 'totalChunks': 'number', 'limit': 'number', 'offset': 'number', 'revision': 'number'},
        'upload_chunk': {'data': 'object', 'documentId': 'string', 'documentName': 'string', 'message': 'string', 'revision': 'number'},
        'update_chunk': {'documentId': 'string', 'id': 'string', 'chunkIndex': 'number', 'content': 'string', 'contentLength': 'number', 'tokenCount': None, 'enabled': 'boolean', 'revision': 'number'},
        'delete_chunk': {'documentId': 'string', 'chunkId': 'string', 'message': 'string', 'revision': 'number'},
        'list_tags': {'tags': 'array', 'totalTags': 'number'},
    }[op]
    return {**common, **fields}


def perform(c, job, inputs):
    project = job['project']; op = inputs['operation']; did = inputs.get('documentId')
    if inputs['knowledgeBaseId'] != project: raise ValueError('Knowledge base unavailable in this project.')
    before = None
    if did:
        # Observe revision before lazy legacy ingestion; revision0 is a valid compare value.
        original = knowledge.source(c, project, did)
        before = knowledge.state(c, did, original)['revision']
        if 'expectedRevision' in inputs and before != inputs['expectedRevision']: raise resources.Conflict('Knowledge document changed. Reopen its current revision.')
        knowledge.ensure(c, project, did)
    if op == 'upsert_document' and not did:
        matches = rows(c, resources.DOCS + ' AND v.title=?', project, inputs['name'].strip())
        if len(matches) > 1: raise ValueError('Several documents have this name. Select an exact document ID.')
        if matches:
            did = matches[0]['id']; before = knowledge.state(c, did, matches[0])['revision']
            if 'expectedRevision' in inputs and before != inputs['expectedRevision']: raise resources.Conflict('Knowledge document changed. Reopen its current revision.')
            knowledge.ensure(c, project, did)
        elif 'expectedRevision' in inputs: raise resources.Conflict('No existing document matches the expected revision.')
    result = {'knowledgeBaseId': project}
    if op in ('create_document', 'upsert_document'):
        data = {'title': inputs['name'], 'text': inputs['content']}
        old = knowledge.source(c, project, did) if did else None
        if did: data.update(id=did, expectedVersion=old['version'])
        created = resources.save_document(c, project, job['chat'], data)
        did = created['id']
        if 'documentTags' in inputs:
            knowledge.set_tags(c, project, did, inputs['documentTags'])
            # Save_document's ingest checkpoint is within this same transaction. Enrich that exact revision.
            c.execute('DELETE FROM knowledge_history WHERE document=? AND revision=?', (did, knowledge.state(c, did)['revision']))
            knowledge.checkpoint(c, did)
        meta = knowledge.document_public(c, project, did, True)
        details = dict(documentId=did, documentName=meta['filename'], type='document', enabled=meta['enabled'], createdAt=meta['uploadedAt'], updatedAt=meta['updatedAt'])
        if op == 'upsert_document': details.update(isUpdate=old is not None, previousDocumentId=did if old else None)
        result.update(data=details, documentId=did, message='Updated document.' if old else 'Created document.', revision=meta['revision'])
    elif op == 'list_documents':
        search = inputs.get('search', '').casefold(); enabled = inputs.get('enabledFilter', 'all')
        found = []
        # Count and page use identical SQL filters; inspect only the requested bounded page.
        clause = " AND instr(lower(v.title),lower(?))>0 AND (?='all' OR coalesce(s.enabled,1)=?)"
        sql = resources.DOCS.replace(' WHERE k.project=?', ' LEFT JOIN knowledge_state s ON s.document=k.id WHERE k.project=?') + clause
        args = (project, search, enabled, int(enabled == 'enabled'))
        total = row(c, 'SELECT count(*) AS n FROM (' + sql + ')', *args)['n']
        limit = inputs.get('limit', 50); offset = inputs.get('offset', 0)
        for rec in rows(c, sql + ' ORDER BY v.updated DESC,k.id LIMIT ? OFFSET ?', *args, limit, offset): found.append(knowledge.document_public(c, project, rec['id']))
        result.update(documents=found, totalDocuments=total, limit=limit, offset=offset)
    elif op == 'get_document': result.update(knowledge.document_public(c, project, did, True))
    elif op == 'delete_document':
        knowledge.touch(c, did, deleted=1)
        knowledge.checkpoint(c, did)
        result.update(documentId=did, message='Deleted document from active knowledge.', revision=knowledge.state(c, did)['revision'])
    elif op == 'list_tags':
        found = knowledge.definitions(c, project); result.update(tags=found, totalTags=len(found))
    elif op == 'list_chunks':
        enabled = inputs.get('enabledFilter', 'all'); search = inputs.get('search', '').casefold()
        found = [r for r in knowledge.chunks(c, did) if search in r['content'].casefold() and (enabled == 'all' or bool(r['enabled']) == (enabled == 'enabled'))]
        limit = inputs.get('limit', 50); offset = inputs.get('offset', 0)
        result.update(documentId=did, chunks=[knowledge.chunk_public(r) for r in found[offset:offset + limit]], totalChunks=len(found), limit=limit, offset=offset, revision=knowledge.state(c, did)['revision'])
    else:
        current = knowledge.chunks(c, did)
        if op == 'upload_chunk':
            if len(current) >= 128: raise ValueError('A local knowledge document supports at most 128 active chunks.')
            chunk_id = uid(); index = row(c, 'SELECT coalesce(max(chunk_index),-1)+1 AS n FROM knowledge_chunks WHERE document=?', did)['n']
            c.execute('INSERT INTO knowledge_chunks VALUES(?,?,?,?,1,0,?,?)', (chunk_id, did, index, inputs['content'], now(), now()))
        else:
            chunk_id = inputs['chunkId']
            selected = next((r for r in current if r['id'] == chunk_id), None)
            if selected is None: raise ValueError('Chunk unavailable in this document.')
            if op == 'update_chunk':
                c.execute('UPDATE knowledge_chunks SET content=?,enabled=?,updated=? WHERE id=?', (inputs.get('content', selected['content']), int(inputs.get('enabled', bool(selected['enabled']))), now(), chunk_id))
            else: c.execute('UPDATE knowledge_chunks SET deleted=1,updated=? WHERE id=?', (now(), chunk_id))
        # Bound total active chunk bytes, not just one input; failed mutation rolls back with no receipt.
        total = row(c, 'SELECT coalesce(sum(length(cast(content AS blob))),0) AS n FROM knowledge_chunks WHERE document=? AND deleted=0', did)['n']
        if total > 131072: raise ValueError('Local knowledge chunks exceed the 128 KiB per-document limit.')
        knowledge.touch(c, did); knowledge.checkpoint(c, did)
        result.update(documentId=did, revision=knowledge.state(c, did)['revision'])
        if op == 'delete_chunk': result.update(chunkId=chunk_id, message='Deleted chunk.')
        else:
            info = knowledge.chunk_public(row(c, 'SELECT * FROM knowledge_chunks WHERE id=?', chunk_id))
            if op == 'upload_chunk': result.update(data={**info, 'chunkId': chunk_id}, documentName=knowledge.source(c, project, did)['title'], message='Added chunk.')
            else: result.update(info)
    graph_values.bound(result)
    if op in MUTATIONS:
        resources.audit(c, project, job['chat'], 'Knowledge', op.replace('_', ' ').capitalize(), 'Document ' + did + ' · revision ' + str(result['revision']))
    return dict(status='done', port='success', detail='Knowledge ' + op.replace('_', ' ') + ' completed.', values=result,
                documentRevisionBefore=before, documentRevisionAfter=knowledge.state(c, did)['revision'] if did else None,
                processing=dict(route='local-knowledge', embedding=False, tokenizer=False, chunking='4000 Unicode characters, no overlap'))
