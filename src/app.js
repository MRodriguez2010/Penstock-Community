import {arrangeWorkflow} from './canvasLayout.js';
import {planPipes, pipePath} from './pipeRouting.js';
import {executionOverlay} from './workflowEvidence.js';

import {projectPanel} from './projects.js';

const $ = id => document.getElementById(id);
const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const button = (text, action, cls) => {
  const b = el('button', text, cls);
  b.addEventListener('click', () => attempt(action));
  return b;
};
let current, trace, catalog, selected, editing, busy = false;
let key = sessionStorage.getItem('penstock.key') || '';
let pendingStart, projectId;
const panel = projectPanel({api,attempt,el,button,openRun,project:()=>projectId,versions:()=>catalog.versions,services:()=>catalog.serviceProtocols,
  draft:async(plan,sample)=>{
    await author(false);
    $('proposal').value=JSON.stringify(plan,null,2);
    $('inputFormat').value='JSON'; $('input').value=JSON.stringify(sample,null,2);
  }});
function notice(text) {
  $('notice').textContent = text;
  $('notice').hidden = !text;
}
async function attempt(action) {
  if (busy) return;
  busy = true;
  notice('');
  try { await action(); }
  catch (error) { notice(error.message); }
  finally { busy = false; }
}
async function api(path, data) {
  const options = {headers: {'Authorization': 'Bearer ' + key}, cache:'no-store'};
  if (data !== undefined) Object.assign(options, {method: 'POST', body: JSON.stringify(data),
    headers:{...options.headers,'Content-Type':'application/json'}});
  const response = await fetch('/api/' + path, options);
  const result = await response.json();
  if (!response.ok) throw Error(result.error || 'Request failed.');
  return result;
}
function item(text, fn, active) {return button(text, fn, 'item' + (active ? ' on' : ''));}
async function refresh() {
  catalog = projectId ? await api('project',{project:projectId}) : await api('workspace');
  projectId = catalog.project;
  $('blockKinds').textContent=JSON.stringify(catalog.blocks,null,2);
  $('project').replaceChildren(...catalog.projects.map(p=>new Option(p.name,p.id,false,p.id===projectId)));
  $('library').replaceChildren(...catalog.versions.map(v => item(v.name + ' · v' + v.version,
    () => openVersion(v.workflow, v.version), current?.workflow === v.workflow && current.version === v.version)));
  $('history').replaceChildren(...catalog.runs.map(r => item(r.mode + ' · ' + r.state + ' · ' + r.id.slice(0,8),
    () => openRun(r.id), trace?.id === r.id)));
  const names = Object.keys(catalog.providers);
  $('provider').replaceChildren(...names.map(name => el('option',name)));
  $('generate').disabled = !names.length;
  $('offline').hidden = !!names.length;
  $('catalog').replaceChildren(...['providers','skills','services'].map(kind => {
    const items = kind === 'providers' ? names : catalog[kind];
    return el('p', kind + ' · ' + (items.join(', ') || 'None configured'));
  }));
}
async function unlock() {
  await refresh();
  sessionStorage.setItem('penstock.key', key);
  $('login').hidden = true; $('studio').hidden = false; $('lock').hidden = false; $('owner').value = '';
  const first = catalog.versions[0];
  if (first) await openVersion(first.workflow, first.version);
}
$('unlock').onsubmit = e => {e.preventDefault(); key = $('owner').value.trim(); attempt(unlock);};
$('lock').onclick = () => {sessionStorage.removeItem('penstock.key'); location.reload();};
async function openVersion(workflow,version) {
  $('resources').hidden=true;
  current = await api('version',{workflow,version});
  selected = current.graph.nodes[0].id;
  trace = null; pendingStart = null;
  $('work').hidden = false; $('author').hidden = true;
  await refresh(); render();
}
async function openRun(id) {
  const run = await api('run',{id});
  await openVersion(run.workflow,run.version);
  trace = run; selected = run.cursor || run.history.at(-1)?.node || selected;
  await refresh(); render();
}
function render() {
  $('revision').textContent = 'Version ' + current.version + ' · ' + (current.published ? 'Published' : 'Draft');
  $('name').textContent = current.name;
  $('run').disabled = !current.published;
  $('publish').disabled = !!current.published || !catalog.runs.some(r => r.workflow === current.workflow &&
    r.version === current.version && r.state === 'finished' && r.mode === 'test') && !(trace?.state === 'finished' && trace.mode === 'test');
  drawGraph(); drawReceipt();
}
function drawGraph() {
  const {nodes, edges} = current.graph;
  const measurements = Object.fromEntries(nodes.map(n => [n.id,{width:176,height:64}]));
  const positions = arrangeWorkflow({nodes,edges,measurements,mode:$('layout').value});
  const boxes = nodes.map(n => {const {x,y} = positions.get(n.id);
    return {id:n.id,left:x,right:x+176,top:y,bottom:y+64};});
  const box = new Map(boxes.map(b => [b.id,b]));
  const pipes = edges.map(e => {
    const a = box.get(e.source), b = box.get(e.target);
    return {id:e.id,sourceId:e.source,targetId:e.target,source:{x:a.right,y:a.top+32},
      target:{x:b.left,y:b.top+32},labelWidth:e.sourcePort === 'success' ? 0 : 60};
  });
  const plans = planPipes(boxes,pipes);
  const canvas = $('canvas');
  const width = Math.max(...boxes.map(b => b.right), ...[...plans.values()].flatMap(p => p.points.map(v => v.x))) + 48;
  const height = Math.max(...boxes.map(b => b.bottom), ...[...plans.values()].flatMap(p => p.points.map(v => v.y))) + 50;
  canvas.style.width = width+'px'; canvas.style.height = height+'px';
  function svg(tag,attrs) {const n=document.createElementNS('http://www.w3.org/2000/svg',tag);
    for(const [k,v] of Object.entries(attrs)) n.setAttribute(k,String(v));return n;}
  const lines = svg('svg',{width,height,'aria-hidden':'true'});
  const overlay = trace && executionOverlay({...trace,run:trace.id},trace.id,current.workflow,current.graph);
  const receipts = new Map((overlay?.history || []).map(step => [step.node,step]));
  for (const edge of edges) {
    const route = plans.get(edge.id), taken = receipts.get(edge.source)?.port === edge.sourcePort;
    lines.append(svg('path',{d:pipePath(route.points,route.radii),stroke:taken?'#87cbab':'#878084',
      fill:'none','stroke-width':taken?3:2}));
    if (route.label) {
      const label = svg('text',{x:route.label.x,y:route.label.y-6,'text-anchor':'middle',fill:'#e4c3bb','font-size':11});
      label.textContent = edge.sourcePort; lines.append(label);
    }
  }
  canvas.replaceChildren(lines);
  for (const n of nodes) {
    const b = button('',() => {selected=n.id;drawGraph();},'node');
    b.style.left=box.get(n.id).left+'px'; b.style.top=box.get(n.id).top+'px';
    b.title = n.label || n.id;
    b.classList.toggle('visited',receipts.has(n.id)); b.classList.toggle('active',selected===n.id);
    b.classList.toggle('waiting',trace?.state==='waiting' && trace.cursor===n.id);
    b.append(el('small',n.type),el('strong',n.label || n.id));canvas.append(b);
  }
  const n=nodes.find(n=>n.id===selected) || nodes[0];
  const detail=$('inspector');
  detail.replaceChildren(el('h2',n.label || n.id),el('p',n.type+' · '+n.id));
  if (Object.keys(n.config).length) detail.append(el('pre',JSON.stringify(n.config,null,2)));
  if (receipts.has(n.id)) detail.append(el('pre',JSON.stringify(receipts.get(n.id),null,2)));
}
function drawReceipt() {
  $('receipt').hidden = !trace;
  if (!trace) return;
  $('status').textContent=trace.mode+' · '+trace.state;
  const pending=trace.context.pending;
  $('decision').replaceChildren();
  if(trace.state==='waiting' && pending) {
    $('decision').append(el('strong',pending.question),el('pre',JSON.stringify(pending.value,null,2)));
    const actions=el('div',null,'actions');
    let answer;
    if(pending.answerRequired) {
      const field=el('label','Answer (JSON)');
      answer=el('textarea'); field.append(answer); $('decision').append(field);
    }
    if(pending.operation) $('decision').append(el('pre',JSON.stringify(pending.operation,null,2)));
    for(const [label,approve] of [['Approve',true],['Decline',false]]) actions.append(button(label,async()=>{
      const updated=await api('decide',{id:trace.id,node:pending.node,approve,answer:answer && approve ? JSON.parse(answer.value) : undefined});
      await openRun(updated.id);
    }));
    $('decision').append(actions);
  }
  if(trace.context.childRuns) {
    for(const id of trace.context.childRuns) $('decision').append(button('Open child '+id.slice(0,8),()=>openRun(id)));
  }
  $('refreshReceipt').onclick=()=>attempt(()=>openRun(trace.id));
  $('steps').replaceChildren(...trace.history.map(s=>el('li',s.node+' · '+(s.error || s.decision || s.port || 'finished')+(s.preview?' · delivery preview':''))));
  $('result').textContent=trace.state==='finished' ? JSON.stringify(trace.context.result,null,2) :
    trace.state==='interrupted'?'The server stopped during this run. Inspect the receipt and check external operations before starting a new run.':'';
}
async function author(edit) {
  $('resources').hidden=true;
  editing=edit ? current : null;
  $('proposal').value=JSON.stringify(edit ? current.plan : await (await fetch('/examples/review.json')).json(),null,2);
  $('brief').value=''; $('work').hidden=true; $('author').hidden=false;
}
$('new').onclick=()=>attempt(()=>author(false));
$('edit').onclick=()=>attempt(()=>author(true));
$('cancel').onclick=()=>{$('work').hidden=false; $('author').hidden=true;};
$('generate').onclick=()=>attempt(async()=>{
  const result=await api('propose',{brief:$('brief').value,includeResources:$('includeResources').checked,project:projectId,provider:$('provider').value});
  $('proposal').value=JSON.stringify(result.plan,null,2); notice('Proposal ready to review.');
});
$('save').onclick=()=>attempt(async()=>{
  const result=await api('save',{plan:JSON.parse($('proposal').value),project:projectId,workflow:editing?.workflow,baseVersion:editing?.version});
  await openVersion(result.workflow,result.version);
});
$('layout').onchange=drawGraph;
$('publish').onclick=()=>attempt(async()=>{
  current=await api('publish',{workflow:current.workflow,version:current.version});render();await refresh();
});
async function start(mode) {
  const fields={workflow:current.workflow,version:current.version,mode,input:$('inputFormat').value==='JSON'?JSON.parse($('input').value):$('input').value};
  const intent=JSON.stringify(fields);
  if(pendingStart?.intent!==intent) pendingStart={intent,id:crypto.randomUUID ? crypto.randomUUID() :
    Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('')};
  const result=await api('start',{...fields,request:pendingStart.id});
  await openRun(result.id);
}
$('test').onclick=()=>attempt(()=>start('test'));
$('run').onclick=()=>attempt(()=>start('live'));
async function changeProject(id) {
  projectId=id; current=null; trace=null; editing=null; pendingStart=null;
  $('work').hidden=true; $('author').hidden=true; $('resources').hidden=true;
  await refresh();
  if(catalog.versions.length) await openVersion(catalog.versions[0].workflow,catalog.versions[0].version);
  else await panel.open();
}
$('project').onchange=()=>attempt(()=>changeProject($('project').value));
$('addProject').onclick=()=>attempt(async()=>{
  const name=prompt('Project name');
  if(name) await changeProject((await api('project-create',{name})).id);
});
$('manageData').onclick=()=>attempt(()=>panel.open());
$('closeResources').onclick=()=>attempt(async()=>{
  $('resources').hidden=true;
  if(current) $('work').hidden=false;
  else await author(false);
});
if(key) attempt(unlock);
