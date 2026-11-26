const $ = id => document.getElementById(id);

function field(label, input) {
  const wrapper = document.createElement('label');
  wrapper.textContent = label;
  wrapper.append(input);
  return wrapper;
}

function input(value = '', type = 'text') {
  const node = document.createElement('input');
  node.type = type;
  node.value = value;
  return node;
}

function options(values) {
  const node = document.createElement('select');
  for (const [value,label] of values) node.add(new Option(label,value));
  return node;
}

function requestId() {
  const bytes=crypto.getRandomValues(new Uint8Array(16));
  bytes[6]=(bytes[6]&15)|64;
  bytes[8]=(bytes[8]&63)|128;
  const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
  return [hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join('-');
}

export function projectPanel({api,attempt,el,button,openRun,project,versions,services,draft}) {
  let tables=[], page=null, offset=0, controls={}, selectedProject;
  const request=(path,fields={})=>api(path,{project:project(),...fields});
  const data=(action,fields)=>request('resource',{action,data:fields});
  const mutate=(action,fields)=>data(action,{...fields,key:requestId()});
  const handle=(id,action)=>{$(id).onclick=()=>attempt(action);};

  async function showTable(id, start=0) {
    offset=start;
    page=await data('table-read',{id,offset,limit:25});
    $('tableView').hidden=false;
    $('tableId').value=id;
    $('tableTitle').textContent=page.table.name+' · '+page.total+' rows';
    const table=el('table'),head=el('tr');
    head.append(...page.table.columns.map(col=>el('th',col)));
    table.append(head);
    for(const record of page.records) {
      const tr=el('tr');
      for(const col of page.table.schema) {
        const cell=el('td'),value=record.values[col.id];
        cell.append(button(value || 'Edit',async()=>{
          const replacement=prompt('Edit '+col.name,value);
          if(replacement===null) return;
          await mutate('table-cell',{id,row:record.id,column:col.id,expectedVersion:record.version,value:replacement});
          await showTable(id,offset);
        }));
        tr.append(cell);
      }
      table.append(tr);
    }
    $('records').replaceChildren(table);
    $('prevPage').disabled=offset===0;
    $('nextPage').disabled=page.nextOffset==null;
    $('rowFields').replaceChildren(...page.table.schema.map(col=>{
      const box=col.type==='select' ? options(['',...col.options].map(v=>[v,v])) : input();
      box.name=col.id;
      return field(col.name,box);
    }));
  }
  function triggerFields() {
    const kind=$('triggerKind').value;
    controls={};
    const children=[];
    const add=(key,label,node)=>{controls[key]=node; children.push(field(label,node));};
    if(kind==='interval') add('seconds','Interval in seconds',input('3600','number'));
    if(kind==='calendar') {
      add('cron','Cron expression',input('0 9 * * 1-5'));
      add('zone','Time zone',input(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'));
    }
    if(['rss','app-trigger'].includes(kind)) {
      const match=kind==='rss'?'rss':'http';
      add('service','Registered service',options(Object.entries(services()).filter(([,p])=>p===match).map(([s])=>[s,s])));
      add('seconds','Poll interval in seconds',input('60','number'));
      if(kind==='app-trigger') {
        add('path','Keys leading to the list, separated by dots (empty for a root array)',input());
        add('idField','Unique item ID field',input('id'));
      }
    }
    if(kind==='table-event') {
      add('table','Table',options(tables.map(v=>[v.id,v.name])));
      add('event','Change',options(['insert','update','delete'].map(v=>[v,v])));
    }
    if(kind==='workspace-event') add('action','Project change',options([
      ['table','Table created'],['table-row','Row added'],['file-save','File saved'],['document-save','Document saved']
    ]));
    $('timingFields').replaceChildren(...children);
    $('payloadLabel').hidden=$('previewSchedule').hidden=!['interval','calendar'].includes(kind);
  }
  function config() {
    switch($('triggerKind').value) {
      case 'rss': return {service:controls.service.value,seconds:Number(controls.seconds.value)};
      case 'app-trigger': return {service:controls.service.value,seconds:Number(controls.seconds.value),
        itemsPath:controls.path.value.split('.').filter(Boolean),idField:controls.idField.value};
      case 'interval': return {seconds:Number(controls.seconds.value)};
      case 'calendar': return {scheduleType:'custom',timezone:controls.zone.value,cronExpression:controls.cron.value};
      case 'table-event': return {tableId:controls.table.value,event:controls.event.value};
      case 'workspace-event': return {actions:[controls.action.value]};
      default: return {};
    }
  }
  async function refresh() {
    const [resources,schedules]=await Promise.all([request('resources'),request('triggers')]);
    tables=resources.tables;
    $('projectAssets').replaceChildren(...[...resources.files,...resources.documents].map(v=>el('p',(v.name || v.title)+' · v'+v.version+' · '+v.id)));
    $('tableList').replaceChildren(...tables.map(t=>button(t.name,()=>showTable(t.id))));
    if(!tables.length) $('tableList').textContent='No project tables yet.';
    const targets=versions().filter(v=>v.published);
    $('target').replaceChildren(...targets.map(v=>new Option(v.name+' · v'+v.version,JSON.stringify([v.workflow,v.version]))));
    $('armTrigger').disabled=!targets.length;
    if(!targets.length) $('target').add(new Option('Publish a tested workflow to enable a trigger',''));
    $('triggers').replaceChildren(...schedules.triggers.map(t=>{
      const workflow=versions().find(v=>v.workflow===t.workflow && v.version===t.version);
      const line=el('p',(workflow?.name || t.workflow)+' · v'+t.version+' · '+t.kind+' · '+(t.enabled?'enabled':'paused')+
        (t.next_due && t.enabled?' · '+new Date(t.next_due*1000).toLocaleString():'')+(t.last_error?' · '+t.last_error:''));
      line.append(' ',button(t.enabled?'Pause':'Resume',async()=>{
        await request('trigger-enable',{id:t.id,enabled:!t.enabled}); await refresh();
      }));
      return line;
    }));
    $('deliveries').replaceChildren(...schedules.deliveries.map(d=>d.run ?
      button(d.state+' · '+d.run.slice(0,8),()=>openRun(d.run)) : el('p',d.state+(d.detail?' · '+d.detail:''))));
    if(!schedules.deliveries.length) $('deliveries').textContent='No deliveries yet.';
    if(selectedProject!==project()) {page=null; selectedProject=project(); $('tableView').hidden=true;}
    triggerFields();
  }
  handle('intakeWorkflow',async()=>{
    const table=page.table;
    const plan={name:'Review rows for '+table.name,steps:[
      {id:'start',type:'start',label:'Receive a row',next:'review'},
      {id:'review',type:'review',config:{question:'Add this row to '+table.name+'?'},next:{approved:'append',declined:'hold'}},
      {id:'append',type:'table',config:{operation:'insert_row',tableId:table.id,data:{ref:{node:'$input'}}},next:'saved'},
      {id:'saved',type:'finish',label:'Row saved'}, {id:'hold',type:'finish',label:'Row declined'}
    ]};
    const examples={text:'Example',number:1,boolean:true,date:'2026-01-01',url:'https://example.org',email:'reader@example.org'};
    const sample=Object.fromEntries(table.schema.map(col=>[col.name,col.type==='select'?col.options[0]:examples[col.type]]));
    await draft(plan,sample);
  });
  handle('createTable',async()=>{
    const table=await mutate('table',{name:$('tableName').value,columns:$('columns').value.split(',').map(v=>v.trim())});
    await refresh(); await showTable(table.id);
  });
  handle('addColumn',async()=>{
    const type=$('columnType').value,id=page.table.id;
    const column={name:$('columnTitle').value,type,unique:$('columnUnique').checked,
      options:type==='select'?$('columnChoices').value.split(',').map(v=>v.trim()):[]};
    await mutate('table-column',{id,column,expectedRevision:page.table.revision});
    await showTable(id,offset);
  });
  handle('saveDocument',async()=>{
    await mutate('document-save',{title:$('docName').value,text:$('docBody').value});
    $('docName').value=''; $('docBody').value=''; await refresh();
  });
  handle('uploadFile',async()=>{
    const file=$('projectFile').files[0];
    if(!file || file.size>700000) throw Error('Choose a project file up to 700,000 bytes.');
    const payload={name:file.name};
    if(/\.(png|jpe?g|webp)$/i.test(file.name)) {
      const bytes=new Uint8Array(await file.arrayBuffer());
      let binary='';
      for(let start=0;start<bytes.length;start+=8192) binary+=String.fromCharCode(...bytes.subarray(start,start+8192));
      payload.base64=btoa(binary);
    } else payload.text=await file.text();
    await mutate('file-save',payload); $('projectFile').value=''; await refresh();
  });
  handle('importTable',async()=>{
    const table=await mutate('table-import',{name:$('tableName').value || 'Imported table',csv:$('csv').value});
    await refresh(); await showTable(table.id);
  });
  $('addRow').onsubmit=e=>{
    e.preventDefault();
    attempt(async()=>{
      const id=page.table.id;
      await mutate('table-row',{id,values:Object.fromEntries(new FormData(e.target)),expectedRevision:page.table.revision});
      await showTable(id,offset);
    });
  };
  handle('prevPage',()=>showTable(page.table.id,Math.max(0,offset-25)));
  handle('nextPage',()=>showTable(page.table.id,page.nextOffset));
  handle('exportTable',async()=>{
    const file=await data('table-export',{id:page.table.id});
    const link=el('a'),url=URL.createObjectURL(new Blob([file.csv],{type:'text/csv;charset=utf-8'}));
    link.href=url; link.download=file.name; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  $('triggerKind').onchange=triggerFields;
  handle('previewSchedule',async()=>{
    const result=await request('schedule-preview',{config:config()});
    $('triggerResult').textContent=result.occurrences.map(v=>new Date(v*1000).toLocaleString()).join('\n');
  });
  handle('armTrigger',async()=>{
    const [workflow,version]=JSON.parse($('target').value);
    const kind=$('triggerKind').value,timed=['calendar','interval'].includes(kind);
    const result=await request('trigger-create',{workflow,version,kind:timed?'schedule':kind,config:config(),
      input:timed?JSON.parse($('triggerInput').value):null});
    $('triggerResult').textContent=result.token ? 'Webhook '+result.path+'\nBearer token (shown once): '+result.token : 'Trigger enabled.';
    await refresh();
  });
  handle('reloadResources',async()=>{await refresh(); if(page) await showTable(page.table.id,offset);});
  return {open:async()=>{
    $('work').hidden=true; $('author').hidden=true; $('resources').hidden=false;
    $('triggerResult').textContent='';
    await refresh();
  }};
}
