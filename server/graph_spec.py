"""Expand a workflow proposal into the versioned canvas graph."""
import copy
import hashlib
import json
import re


def graph_id(v):
    if not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',v):raise ValueError('Use a stable alphanumeric node or edge ID.')
    return v

def digest(graph):
    # Notes have their own saved presentation per version and no execution ports.
    value={key:val for key,val in graph.items() if key!='annotations'}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def graph_spec(body,children):
    """Expand positions, edges and declared child aliases into the ordinary Studio wire format."""
    if not isinstance(body,list) or not 1<=len(body)<=40:raise ValueError('Provide one to forty linked steps.')
    nodes=[];edges=[]
    for i,step in enumerate(body):
        if not isinstance(step,dict) or set(step)-{'id','type','config','revision','next','label','parent'}:raise ValueError('Unexpected step field.')
        key=graph_id(step['id']);kind=step['type'];cfg=copy.deepcopy(step.get('config',{}))
        if not isinstance(cfg,dict):raise ValueError('Step configuration must be an object.')
        if kind in ('for-each','parallel-each','invoke-workflow') and isinstance(cfg.get('workflow'),str) and cfg['workflow'].startswith('@'):
            alias=cfg['workflow'][1:]
            if alias not in children:raise ValueError('Unknown child workflow '+alias+'.')
            cfg.update(workflow=children[alias]['id'],version=children[alias]['version'])
        node=dict(id=key,type=kind,position={'x':(i%5)*270,'y':(i//5)*170},config=cfg)
        if 'revision' in step:node['configRevision']=step['revision']
        if 'label' in step:node['label']=step['label']
        if 'parent' in step:node['parent']=graph_id(step['parent'])
        nodes.append(node)
        links=step.get('next',{})
        if isinstance(links,str):links={'success':links}
        if not isinstance(links,dict):raise ValueError('Map each output to a destination ID or a list of destination IDs.')
        for port,selection in links.items():
            targets=selection if isinstance(selection,list) else [selection]
            if not targets or len(edges)+len(targets)>100:raise ValueError('Use nonempty destination lists and at most 100 connections.')
            for target in targets:
                edges.append(dict(id='link-'+str(len(edges)+1),source=key,target=graph_id(target),sourcePort=port,targetPort='in'))
    # The first authoring slice has one success lane and a dedicated error terminal.
    kinds=[n['type'] for n in nodes]
    if len(kinds)==5 and kinds.count('finish')==2 and 'agent' in kinds and 'verify-value' in kinds and ('start' in kinds or 'n8n-schedule' in kinds):
        columns={'start':0,'n8n-schedule':0,'agent':1,'verify-value':2,'finish':3}
        error_nodes=[n for n in nodes if n['type']=='finish' and
                     {e['sourcePort'] for e in edges if e['target']==n['id']}=={'error'}]
        if len(error_nodes)==1:
            for n in nodes:
                n['position']={'x':270*columns[n['type']],'y':185 if n is error_nodes[0] else 0}
    return dict(nodes=nodes,edges=edges)
