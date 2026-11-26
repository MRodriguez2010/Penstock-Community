"""Project-scoped, versioned workspace resources."""
import csv
import hashlib
import io
import json
import uuid
from pathlib import PurePosixPath
from store import now, row, rows, uid

class Missing(ValueError): pass
class Conflict(ValueError): pass

def ident(value):
    return str(uuid.UUID(str(value)))

def bounded(value,label,limit,empty=False):
    if not isinstance(value,str) or '\x00' in value or len(value)>limit or (not empty and not value.strip()):
        raise ValueError(f'Invalid {label} (maximum {limit} characters).')
    return value

def name(value,label):
    value=bounded(value,label,120).strip()
    if any(ord(ch)<32 or ord(ch)==127 for ch in value):raise ValueError(f'Invalid {label}.')
    return value

def page(query,default=150,maximum=250):
    limit=int(query.get('limit',default))
    offset=int(query.get('offset',0))
    if not 1<=limit<=maximum or not 0<=offset<=1000000:raise ValueError('Invalid page.')
    return limit,offset

def audit(c,p,chat,source,title,detail,tool='Penstock',host='workspace',status='completed'):
    c.execute('INSERT INTO resource_log(project,chat,at,source,tool,host,status,title,detail) VALUES(?,?,?,?,?,?,?,?,?)',
              (p,chat,now(),source,tool,host,status,title,detail))

def cached(c,chat,d,action):
    key=ident(d['key'])
    args={k:v for k,v in d.items() if k not in ('key','chat')}
    fingerprint=hashlib.sha256(json.dumps(args,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    previous=row(c,'SELECT action,fingerprint,result FROM resource_requests WHERE chat=? AND request=?',chat,key)
    if previous and (previous['action'],previous['fingerprint'])!=(action,fingerprint):
        raise Conflict('Request ID already belongs to a different change.')
    return key,fingerprint,json.loads(previous['result']) if previous else None

def remember(c,chat,key,action,fingerprint,result):
    c.execute('INSERT INTO resource_requests VALUES(?,?,?,?,?)',(chat,key,action,fingerprint,json.dumps(result)))
    return result

MEDIA={'glb':'model/gltf-binary','png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg','webp':'image/webp','gif':'image/gif','mp3':'audio/mpeg',
       'wav':'audio/wav','m4a':'audio/mp4','ogg':'audio/ogg','webm':'audio/webm','flac':'audio/flac'}
MEDIA_LIMIT=10*1048576

def media_type(filename):
    return MEDIA.get(filename.rsplit('.',1)[-1].lower()) if '.' in filename else None

def save_media(c,p,chat,filename,raw,source='Files'):
    filename=name(filename,'file name')
    if (PurePosixPath(filename).name!=filename or '\\' in filename or filename.startswith('.') or not media_type(filename)):
        raise ValueError('Use a GLB mesh, image (png, jpg, webp, gif) or audio (mp3, wav, m4a, ogg, webm, flac) filename without a path.')
    if not raw or len(raw)>MEDIA_LIMIT:raise ValueError('A project media file is limited to 10 MiB.')
    version=row(c,'SELECT coalesce(max(version),0)+1 AS n FROM project_files WHERE project=? AND name=?',p,filename)['n']
    fid=uid();digest=hashlib.sha256(raw).hexdigest()
    c.execute('INSERT INTO project_files VALUES(?,?,?,?,?,?,?,?)',(fid,p,chat,filename,version,digest,raw,now()))
    audit(c,p,chat,source,'Saved '+filename,f'Version {version} · {len(raw)} bytes · SHA-256 {digest}.')
    return {'id':fid,'version':version,'name':filename,'bytes':len(raw),'digest':digest}

def save_file(c,p,chat,d):
    if 'base64' in d:
        import base64,binascii
        if not isinstance(d['base64'],str) or len(d['base64'])>MEDIA_LIMIT*4//3+8:raise ValueError('A project media file is limited to 10 MiB.')
        try:raw=base64.b64decode(d['base64'],validate=True)
        except (binascii.Error,ValueError):raise ValueError('Upload data is not valid base64.') from None
        saved=save_media(c,p,chat,d.get('name'),raw)
        return {'id':saved['id'],'version':saved['version']}
    filename=name(d.get('name'),'file name')
    if (filename in ('.','..') or PurePosixPath(filename).name!=filename or '\\' in filename or
        filename.startswith('.') or filename.rsplit('.',1)[-1].lower() not in ('txt','md','csv','json','log','tsv','yaml','yml')):
        raise ValueError('Use a plain text, Markdown, CSV, TSV, JSON, YAML or log filename without a path.')
    raw=bounded(d.get('text'),'file text',65536,True).encode()
    if len(raw)>65536:raise ValueError('A project text file is limited to 64 KiB.')
    version=row(c,'SELECT coalesce(max(version),0)+1 AS n FROM project_files WHERE project=? AND name=?',p,filename)['n']
    fid=uid()
    digest=hashlib.sha256(raw).hexdigest()
    c.execute('INSERT INTO project_files VALUES(?,?,?,?,?,?,?,?)',(fid,p,chat,filename,version,digest,raw,now()))
    audit(c,p,chat,'Files','Saved '+filename,f'Version {version} · {len(raw)} bytes · SHA-256 {digest}.')
    return {'id':fid,'version':version}

DOCS='''SELECT k.id,k.chat,k.version,v.title,v.text,v.updated FROM knowledge_docs k
        JOIN knowledge_versions v ON v.document=k.id AND v.version=k.version WHERE k.project=?
        AND NOT EXISTS(SELECT 1 FROM knowledge_state ks WHERE ks.document=k.id AND ks.deleted=1)'''

def documents(c,p,q='',limit=150,offset=0):
    q=bounded(q,'search query',180,True).strip()
    return rows(c,"SELECT id,chat,version,title,substr(text,1,180) AS excerpt,updated FROM ("+DOCS+
                ") WHERE instr(lower(title),lower(?))>0 OR instr(lower(text),lower(?))>0 ORDER BY updated DESC,id LIMIT ? OFFSET ?",p,q,q,limit,offset)

def document(c,p,doc_id):
    record=row(c,DOCS+' AND k.id=?',p,doc_id)
    if not record:raise Missing('Document unavailable in this project.')
    return record

def save_document(c,p,chat,d):
    title=name(d.get('title'),'document title')
    body=bounded(d.get('text'),'document text',32000,True)
    if d.get('id'):
        doc_id=ident(d['id'])
        current=document(c,p,doc_id)
        if type(d.get('expectedVersion')) is not int or d['expectedVersion']!=current['version']:
            raise Conflict('This document has a newer version. Reopen it before saving.')
        version=current['version']+1
        c.execute('UPDATE knowledge_docs SET version=? WHERE id=?',(version,doc_id))
    else:
        doc_id=uid();version=1
        c.execute('INSERT INTO knowledge_docs VALUES(?,?,?,?)',(doc_id,p,chat,version))
    c.execute('INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?)',(doc_id,version,chat,title,body,now()))
    from knowledge_store import ingest
    ingest(c,p,doc_id)
    audit(c,p,chat,'Knowledge Base','Saved '+title,f'Document version {version}.')
    return {'id':doc_id,'version':version}

def managed_table(c,p,table_id):
    record=row(c,"""SELECT t.id,t.name,t.chat,t.updated,t.columns_json,'project' AS kind,NULL AS run,
               (SELECT count(*) FROM project_records v WHERE v.table_id=t.id) AS rowCount
               FROM project_tables t WHERE t.project=? AND t.id=?""",p,table_id)
    if not record:raise Missing('Editable table unavailable in this project.')
    record['columns']=json.loads(record.pop('columns_json'))
    return record

def file_data(c,p,file_id):
    result=row(c,"SELECT *, 'project' AS kind FROM project_files WHERE id=? AND project=?",file_id,p)
    if not result:raise Missing('File unavailable in this project.')
    raw=result.pop('data')
    if hashlib.sha256(raw).hexdigest()!=result['digest']:raise Conflict('Stored file checksum differs.')
    return result,raw

def artifact_table(c,p,table_id):
    record,raw=file_data(c,p,table_id)
    if not record['name'].lower().endswith('.csv'):raise Missing('Select a project CSV file.')
    parsed=list(csv.reader(io.StringIO(raw.decode('utf-8-sig')),strict=True))
    if not parsed or not parsed[0] or any(len(v)!=len(parsed[0]) for v in parsed):raise ValueError('Invalid CSV table.')
    record.update(kind='artifact',run=None,updated=record['created'],columns=parsed[0],rowCount=len(parsed)-1)
    return record,parsed[1:]
