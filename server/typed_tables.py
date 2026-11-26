"""Typed, revisioned project tables over the original row IDs and stored cell arrays."""
import csv
import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import re
from urllib.parse import urlsplit
import uuid

import project_resources as res
from store import now, row, rows, uid

TYPES=('text','number','boolean','date','select','url','email')

def stable(table,ordinal):
    return str(uuid.uuid5(uuid.UUID(table),'column:'+str(ordinal)))

def column(spec,assigned=None):
    if isinstance(spec,str):spec={'name':spec}
    if not isinstance(spec,dict):raise ValueError('Expected a column definition.')
    title=res.name(spec.get('name'),'column name')
    kind=spec.get('type','text'); unique=spec.get('unique',False); options=spec.get('options',[])
    if kind not in TYPES or type(unique) is not bool or not isinstance(options,list):raise ValueError('Invalid column settings.')
    if kind=='select':
        if not 1<=len(options)<=100:raise ValueError('Supply 1–100 select options.')
        options=[res.name(v,'select option') for v in options]
        if len(set(options))!=len(options):raise ValueError('Select options must be distinct.')
    elif options:raise ValueError('Only select columns have options.')
    return {'id':assigned or uid(),'name':title,'type':kind,'unique':unique,'options':options}

def validate_schema(cols):
    if not 1<=len(cols)<=24 or len({v['name'].casefold() for v in cols})!=len(cols):raise ValueError('Provide 1–24 distinct column names.')

def migrate(c):
    for t in rows(c,'SELECT id,columns_json FROM project_tables WHERE id NOT IN (SELECT table_id FROM typed_schemas)'):
        cols=[column(n,stable(t['id'],i)) for i,n in enumerate(json.loads(t['columns_json']))]
        c.execute('INSERT INTO typed_schemas VALUES(?,1,?)',(t['id'],json.dumps(cols)))
    c.execute('INSERT OR IGNORE INTO record_versions SELECT id,1 FROM project_records')

def checked(col,value):
    value=res.bounded(value,'cell',4096,True)
    if not value:return ''
    kind=col['type']
    if kind=='text':return value
    if kind=='number':
        try:
            if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',value.strip()) or len(value)>120:raise ValueError()
            n=Decimal(value.strip())
            if not n.is_finite() or abs(n.adjusted())>120:raise ValueError()
            text=format(n,'f')
            return ('0' if n==0 else text.rstrip('0').rstrip('.') if '.' in text else text)
        except (ValueError,InvalidOperation):pass
    elif kind=='boolean' and value.lower() in ('true','false'):return value.lower()
    elif kind=='date':
        try:
            if datetime.date.fromisoformat(value).isoformat()==value:return value
        except ValueError:pass
    elif kind=='select' and value in col['options']:return value
    elif kind=='url':
        try:
            parsed=urlsplit(value)
            if parsed.scheme in ('http','https') and parsed.hostname and not any(c.isspace() for c in value):return value
        except ValueError:pass
    elif kind=='email' and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):return value
    raise ValueError(f"Invalid {kind} value in {col['name']}.")

def unique_sets(cols,records):
    seen={c['id']:set() for c in cols if c['unique']}
    for record in records:check_unique(cols,record,seen)
    return seen

def check_unique(cols,values,seen):
    for col,value in zip(cols,values):
        if not col['unique'] or value=='':continue
        used=seen[col['id']]
        if value in used:raise res.Conflict('Duplicate value in '+col['name']+'.')
        used.add(value)

def editable(c,p,tid):
    meta=res.managed_table(c,p,tid)
    schema=row(c,'SELECT revision,schema_json FROM typed_schemas WHERE table_id=?',tid)
    meta.update(revision=schema['revision'],schema=json.loads(schema['schema_json']))
    meta['sources']=rows(c,'SELECT i.artifact,i.digest,i.revision,i.row_count AS rowCount,NULL AS run,a.version FROM table_imports i LEFT JOIN project_files a ON a.id=i.artifact WHERE i.table_id=? ORDER BY i.rowid',tid)
    return meta

def table_exists(c,p,tid):
    return row(c,'SELECT id FROM project_tables WHERE project=? AND id=?',p,tid)

def metadata(c,p,tid):
    if table_exists(c,p,tid):return editable(c,p,tid)
    art,_=res.artifact_table(c,p,tid)
    art['schema']=[column({'name':n,'type':'number' if n in ('quantity','completions') else 'text'},stable(tid,i)) for i,n in enumerate(art['columns'])]
    art['revision']=art['version']
    return art

def raw_records(c,tid,limit=-1,offset=0):
    return [(r['id'],r['version'],json.loads(r['values_json'])) for r in rows(c,
        'SELECT v.id,s.version,v.values_json FROM project_records v JOIN record_versions s ON s.record=v.id WHERE v.table_id=? ORDER BY v.rowid LIMIT ? OFFSET ?',tid,limit,offset)]

def views(c,p,tid):
    return [{**json.loads(v['definition']),'id':v['id'],'name':v['name'],'version':v['version']} for v in
            rows(c,'SELECT * FROM table_views WHERE project=? AND table_id=? ORDER BY rowid',p,tid)]

def selected_view(c,p,tid,vid):
    v=row(c,'SELECT * FROM table_views WHERE id=? AND project=? AND table_id=?',res.ident(vid),p,tid)
    if not v:raise res.Missing('View unavailable in this table.')
    return {**json.loads(v['definition']),'id':v['id'],'name':v['name'],'version':v['version']}

def data(c,p,tid,limit,offset,vid=None,config=None):
    t=metadata(c,p,tid)
    ids=[v['id'] for v in t['schema']]
    view=view_config(t,config) if config is not None else selected_view(c,p,tid,vid) if vid else None
    direct=t['kind']=='project' and not (view and (view['sort'] or view['filter']))
    if direct:records=raw_records(c,tid,limit,offset)
    elif t['kind']=='project':records=raw_records(c,tid)
    else:records=[(str(uuid.uuid5(uuid.UUID(tid),'row:'+str(i))),t['revision'],v) for i,v in enumerate(res.artifact_table(c,p,tid)[1])]
    if view and view['filter']:
        f=view['filter']; pos=ids.index(f['column'])
        def matches(values):
            val=values[pos]
            if f['op']=='empty':return val==''
            if f['op']=='equals':return val==f['value']
            return f['value'].casefold() in val.casefold()
        records=[rec for rec in records if matches(rec[2])]
    if view and view['sort']:
        s=view['sort'];pos=ids.index(s['column']); numeric=t['schema'][pos]['type']=='number'
        filled=[rec for rec in records if rec[2][pos]!='']
        blank=[rec for rec in records if rec[2][pos]=='']
        filled.sort(key=lambda rec:Decimal(rec[2][pos]) if numeric else rec[2][pos].casefold(),reverse=s['direction']=='desc')
        records=filled+blank
    count=t['rowCount'] if direct else len(records)
    subset=records if direct else records[offset:offset+limit]
    return {'table':t,'rows':[v for _,_,v in subset],
            'records':[{'id':rid,'version':rv,'values':dict(zip(ids,v))} for rid,rv,v in subset],
            'views':views(c,p,tid),'view':view,'total':count,'nextOffset':offset+limit if offset+len(subset)<count else None}

def expected(value,current,label):
    if type(value) is not int or value!=current:raise res.Conflict(label+' has changed. Reopen it before saving.')

def bump(c,tid):
    c.execute('UPDATE project_tables SET updated=? WHERE id=?',(now(),tid))
    c.execute('UPDATE typed_schemas SET revision=revision+1 WHERE table_id=?',(tid,))
    return row(c,'SELECT revision FROM typed_schemas WHERE table_id=?',tid)['revision']

def new_table(c,p,chat,d):
    title=res.name(d.get('name'),'table name')
    if not isinstance(d.get('columns'),list):raise ValueError('Supply table columns.')
    cols=[column(s) for s in d['columns']]
    validate_schema(cols)
    tid=uid()
    c.execute('INSERT INTO project_tables VALUES(?,?,?,?,?,?)',(tid,p,chat,title,json.dumps([v['name'] for v in cols]),now()))
    c.execute('INSERT INTO typed_schemas VALUES(?,1,?)',(tid,json.dumps(cols)))
    res.audit(c,p,chat,'Tables','Created '+title,f'{len(cols)} columns.')
    return {'id':tid,'revision':1}

def values_for(cols,received):
    if isinstance(received,dict):
        if set(received)-{v['id'] for v in cols}:raise ValueError('Unknown column in row.')
        received=[received.get(v['id'],'') for v in cols]
    if not isinstance(received,list) or len(received)!=len(cols):raise ValueError('Supply one value per column.')
    return [checked(col,v) for col,v in zip(cols,received)]

def insert_record(c,tid,chat,values):
    rid=uid()
    c.execute('INSERT INTO project_records VALUES(?,?,?,?,?)',(rid,tid,chat,json.dumps(values),now()))
    c.execute('INSERT INTO record_versions VALUES(?,1)',(rid,))
    from graph_table_events import capture
    project=row(c,'SELECT project FROM project_tables WHERE id=?',tid)['project']
    capture(c,project,editable(c,project,tid),'insert',rid,values)
    return rid

def append(c,p,chat,d):
    tid=res.ident(d['id']);t=editable(c,p,tid);cols=t['schema']
    if 'expectedRevision' in d:expected(d['expectedRevision'],t['revision'],'Table')
    values=values_for(cols,d['values'])
    if any(col['unique'] for col in cols):
        seen=unique_sets(cols,(v for _,_,v in raw_records(c,tid)))
        check_unique(cols,values,seen)
    rid=insert_record(c,tid,chat,values)
    rev=bump(c,tid)
    res.audit(c,p,chat,'Tables','Added a row to '+t['name'],f"Row {t['rowCount']+1}.")
    return {'id':rid,'table':tid,'version':1,'revision':rev}

def cell(c,p,chat,d):
    tid=res.ident(d['id']); t=editable(c,p,tid); cols=t['schema'];rid=res.ident(d['row'])
    colid=res.ident(d['column'])
    index=next((i for i,col in enumerate(cols) if col['id']==colid),None)
    if index is None:raise res.Missing('Column unavailable.')
    rec=row(c,'SELECT r.values_json,v.version FROM project_records r JOIN record_versions v ON v.record=r.id WHERE r.id=? AND r.table_id=?',rid,tid)
    if not rec:raise res.Missing('Row unavailable.')
    expected(d.get('expectedVersion'),rec['version'],'Row')
    batch=json.loads(rec['values_json'])
    value=checked(cols[index],d['value'])
    if batch[index]==value:return {'id':rid,'version':rec['version'],'revision':t['revision']}
    old_values=batch.copy()
    batch[index]=value
    if cols[index]['unique'] and value and any(v[index]==value for other,_,v in raw_records(c,tid) if other!=rid):
        raise res.Conflict('Duplicate value in '+cols[index]['name']+'.')
    c.execute('UPDATE project_records SET values_json=? WHERE id=?',(json.dumps(batch),rid))
    c.execute('UPDATE record_versions SET version=version+1 WHERE record=?',(rid,))
    rev=bump(c,tid)
    res.audit(c,p,chat,'Tables','Updated '+t['name'],cols[index]['name']+' · row '+rid)
    from graph_table_events import capture
    capture(c,p,t,'update',rid,batch,old_values,[colid])
    return {'id':rid,'version':rec['version']+1,'revision':rev}

def replace_record(c,p,chat,tid,rid,version,changes):
    """One whole-row change, shared by live workflow operations and table events."""
    t=editable(c,p,tid);cols=t['schema']
    rec=row(c,'SELECT r.values_json,v.version FROM project_records r JOIN record_versions v ON v.record=r.id WHERE r.id=? AND r.table_id=?',rid,tid)
    if not rec:raise res.Missing('Row unavailable.')
    expected(version,rec['version'],'Row')
    old=json.loads(rec['values_json'])
    values=values_for(cols,{**dict(zip([col['id'] for col in cols],old)),**changes})
    changed=[col['id'] for col,a,b in zip(cols,old,values) if a!=b]
    if not changed:return {'id':rid,'version':version,'revision':t['revision']}
    seen=unique_sets(cols,(v for other,_,v in raw_records(c,tid) if other!=rid))
    check_unique(cols,values,seen)
    c.execute('UPDATE project_records SET values_json=? WHERE id=?',(json.dumps(values),rid))
    c.execute('UPDATE record_versions SET version=version+1 WHERE record=?',(rid,))
    revision=bump(c,tid)
    res.audit(c,p,chat,'Tables','Updated '+t['name'],'Row '+rid+' · '+str(len(changed))+' columns.')
    # The cumulative Table Trigger release installs this module. Pre-trigger builds
    # have no event consumer; do not emit one partial event per edited cell.
    import importlib.util
    if importlib.util.find_spec('graph_table_events') is not None:
        from graph_table_events import capture
        capture(c,p,t,'update',rid,values,old,changed)
    return {'id':rid,'version':version+1,'revision':revision}


def change_column(c,p,chat,d):
    tid=res.ident(d['id']);t=editable(c,p,tid);old=t['schema']
    expected(d.get('expectedRevision'),t['revision'],'Table')
    spec=d['column']
    if not isinstance(spec,dict):raise ValueError('Expected a column definition.')
    cid=res.ident(spec['id']) if spec.get('id') else uid()
    old_ids=[v['id'] for v in old]
    if spec.get('id') and cid not in old_ids:raise res.Missing('Column unavailable.')
    replacement=column(spec,cid)
    cols=[v for v in old if v['id']!=cid]
    idx=d.get('index',old_ids.index(cid) if cid in old_ids else len(cols))
    if type(idx) is not int or not 0<=idx<=len(cols):raise ValueError('Invalid column position.')
    cols.insert(idx,replacement);validate_schema(cols)
    seen=unique_sets(cols,[])
    for rid,_,arr in raw_records(c,tid):
        values=values_for(cols,dict(zip(old_ids,arr)))
        check_unique(cols,values,seen)
        c.execute('UPDATE project_records SET values_json=? WHERE id=?',(json.dumps(values),rid))
        c.execute('UPDATE record_versions SET version=version+1 WHERE record=?',(rid,))
    c.execute('UPDATE typed_schemas SET schema_json=? WHERE table_id=?',(json.dumps(cols),tid))
    c.execute('UPDATE project_tables SET columns_json=? WHERE id=?',(json.dumps([v['name'] for v in cols]),tid))
    rev=bump(c,tid)
    res.audit(c,p,chat,'Tables','Changed columns in '+t['name'],replacement['name']+' · '+replacement['type'])
    return {'id':tid,'column':cid,'revision':rev}

def view_config(t,v):
    if not isinstance(v,dict):raise ValueError('Expected a view configuration.')
    ids=[v['id'] for v in t['schema']]
    visible=v.get('visible',ids)
    if not isinstance(visible,list) or not visible or any(cid not in ids for cid in visible) or len(set(visible))!=len(visible):
        raise ValueError('Choose distinct visible columns.')
    f=v.get('filter');s=v.get('sort')
    if f is not None:
        if not isinstance(f,dict) or f.get('column') not in ids or f.get('op') not in ('contains','equals','empty'):raise ValueError('Invalid view filter.')
        f={'column':f['column'],'op':f['op'],'value':res.bounded(f.get('value',''),'filter',1024,True)}
    if s is not None:
        if not isinstance(s,dict) or s.get('column') not in ids or s.get('direction') not in ('asc','desc'):raise ValueError('Invalid view sort.')
        s={'column':s['column'],'direction':s['direction']}
    return {'visible':visible,'filter':f,'sort':s}

def save_view(c,p,chat,d):
    tid=res.ident(d['id']);t=metadata(c,p,tid);v=d['view']
    config=json.dumps(view_config(t,v))
    title=res.name(v['name'],'view name')
    if v.get('id'):
        current=selected_view(c,p,tid,v['id'])
        expected(d.get('expectedVersion'),current['version'],'View')
        vid=current['id'];version=current['version']+1
        c.execute('UPDATE table_views SET name=?,version=?,definition=?,updated=? WHERE id=?',(title,version,config,now(),vid))
    else:
        vid=uid();version=1
        c.execute('INSERT INTO table_views VALUES(?,?,?,?,?,?,?)',(vid,p,tid,title,version,config,now()))
    res.audit(c,p,chat,'Tables','Saved view '+title,t['name'])
    return {'id':vid,'version':version}

def import_csv(c,p,chat,d):
    source=None
    if d.get('sourceArtifact'):
        if 'csv' in d:raise ValueError('Choose a CSV or an artifact.')
        source,raw=res.file_data(c,p,res.ident(d['sourceArtifact']))
        if not source['name'].lower().endswith('.csv'):raise ValueError('Select a project CSV file.')
        text=raw.decode('utf-8-sig')
    else:
        text=res.bounded(d.get('csv'),'CSV',130000);raw=text.encode()
        text=text.removeprefix('\ufeff')
    try:parsed=list(csv.reader(io.StringIO(text,newline=''),strict=True))
    except csv.Error as exc:raise ValueError('Invalid CSV quoting.') from exc
    if not parsed or not parsed[0] or len(parsed)>5001:raise ValueError('Import up to 5,000 rows with a header.')
    header=parsed[0]
    if d.get('id'):
        tid=res.ident(d['id']); t=editable(c,p,tid)
        expected(d.get('expectedRevision'),t['revision'],'Table')
    else:
        default=source['name'].rsplit('.',1)[0] if source else 'Imported table'
        tid=new_table(c,p,chat,{'name':d.get('name',default),'columns':header})['id']
        t=editable(c,p,tid)
    if header!=t['columns']:raise ValueError('CSV headers must match the table columns in order.')
    cols=t['schema'];seen=unique_sets(cols,(v for _,_,v in raw_records(c,tid)))
    for line in parsed[1:]:
        values=values_for(cols,line);check_unique(cols,values,seen)
        insert_record(c,tid,chat,values)
    rev=bump(c,tid)
    c.execute('INSERT INTO table_imports VALUES(?,?,?,?,?,?)',(uid(),tid,source['id'] if source else None,hashlib.sha256(raw).hexdigest(),len(parsed)-1,rev))
    res.audit(c,p,chat,'Tables','Imported CSV into '+t['name'],f'{len(parsed)-1} rows.')
    return {'id':tid,'revision':rev,'inserted':len(parsed)-1}

def export(c,p,tid,vid,config=None):
    result=data(c,p,tid,2**63-1,0,vid,config)
    schema=result['table']['schema']; by_id={s['id']:s['name'] for s in schema}
    visible=result['view']['visible'] if result['view'] else list(by_id)
    buf=io.StringIO(newline='');writer=csv.writer(buf,lineterminator='\n')
    writer.writerow([by_id[k] for k in visible])
    writer.writerows([r['values'][k] for k in visible] for r in result['records'])
    filename=result['table']['name']
    return (filename if filename.lower().endswith('.csv') else filename+'.csv'),buf.getvalue().encode()

def rename(c,p,chat,d):
    tid=res.ident(d['id'])
    current=editable(c,p,tid)
    expected(d.get('expectedRevision'),current['revision'],'Table')
    title=res.name(d.get('name'),'table name')
    if title==current['name']:return {'id':tid,'name':title,'revision':current['revision']}
    c.execute('UPDATE project_tables SET name=? WHERE id=?',(title,tid))
    revision=bump(c,tid)
    res.audit(c,p,chat,'Tables','Renamed '+current['name'],title)
    return {'id':tid,'name':title,'revision':revision}

def delete_row(c,p,chat,d):
    tid=res.ident(d['id']);rid=res.ident(d['row'])
    table=editable(c,p,tid)
    if d.get('confirmed') is not True:raise ValueError('Confirm removal of the selected row.')
    rec=row(c,'SELECT v.version,r.values_json FROM project_records r JOIN record_versions v ON v.record=r.id WHERE r.table_id=? AND r.id=?',tid,rid)
    if not rec:raise res.Missing('Row unavailable in this table.')
    expected(d.get('expectedVersion'),rec['version'],'Row')
    from graph_table_events import capture
    old_values=json.loads(rec['values_json'])
    capture(c,p,table,'delete',rid,old_values,old_values)
    c.execute('DELETE FROM record_versions WHERE record=?',(rid,))
    c.execute('DELETE FROM project_records WHERE table_id=? AND id=?',(tid,rid))
    revision=bump(c,tid)
    res.audit(c,p,chat,'Tables','Removed a row from '+table['name'],'Row '+rid+' · version '+str(rec['version']))
    return {'id':rid,'table':tid,'revision':revision}

def delete_column(c,p,chat,d):
    tid=res.ident(d['id']);cid=res.ident(d['column']);table=editable(c,p,tid)
    if d.get('confirmed') is not True:raise ValueError('Confirm removal of the selected column.')
    expected(d.get('expectedRevision'),table['revision'],'Table')
    schema=table['schema'];index=next((i for i,col in enumerate(schema) if col['id']==cid),None)
    if index is None:raise res.Missing('Column unavailable in this table.')
    if len(schema)==1:raise res.Conflict('A table needs at least one column.')
    for view in views(c,p,tid):
        if cid in view['visible'] or any((view.get(k) or {}).get('column')==cid for k in ('sort','filter')):
            raise res.Conflict('Update the saved view “'+view['name']+'” before removing this column.')
    removed=schema.pop(index)
    for rid,version,values in raw_records(c,tid):
        values.pop(index)
        c.execute('UPDATE project_records SET values_json=? WHERE id=?',(json.dumps(values),rid))
        c.execute('UPDATE record_versions SET version=? WHERE record=?',(version+1,rid))
    c.execute('UPDATE typed_schemas SET schema_json=? WHERE table_id=?',(json.dumps(schema),tid))
    c.execute('UPDATE project_tables SET columns_json=? WHERE id=?',(json.dumps([col['name'] for col in schema]),tid))
    revision=bump(c,tid)
    res.audit(c,p,chat,'Tables','Removed a column from '+table['name'],removed['name']+' · '+cid)
    return {'id':tid,'column':cid,'revision':revision}

CHANGES={'table':new_table,'table-row':append,'table-cell':cell,'table-column':change_column,'table-view':save_view,'table-import':import_csv,
         'table-row-delete':delete_row,'table-column-delete':delete_column,'table-rename':rename}
