import {arrangeWorkflow} from './canvasLayout.js';
import {planPipes, pipePath} from './pipeRouting.js';
import {executionOverlay} from './workflowEvidence.js';
import {projectPanel} from './projects.js';
import {mountStarterStudio} from './starterStudio.js';
import {mountConnections} from './connectionCenter.js';
import {watchInbox} from './inboxNotice.js';
import {mountManagedAccess} from './managed-access.js';
import {mountTeamSetup} from './teamSetup.js';
import {mountWorkflowLifecycle} from './workflowLifecycle.js';
import {mountReceiptReplay} from './receiptReplay.js';
import {createDraftRecovery, draftRecoveryStatus, element as el, field, inputEditor, labelFor, LOOKUP_BLOCKS, planEditor, readableText,
  requestId, resultView, selectInput, valueEditor, valueView} from './studioFields.js';

const $ = id => document.getElementById(id);
function button(text, action, cls) {
  const node = el('button', text, cls);
  node.type = 'button';
  node.onclick = () => attempt(action);
  return node;
}

let current, trace, catalog, selected, editing, editor, runInput, busy = false;
let key = sessionStorage.getItem('penstock.key') || '';
let pendingStart, projectId, returnToReceipt;
const initialLink = new URLSearchParams(location.search);
const views = ['guidedStarter', 'work', 'author', 'resources', 'connectionsHub', 'teamSetup'];
const PROJECT_READ = 'source-read';
const CONNECTED_SOURCE = {
  'project-files': ['Project files and tables', 'Read local file and table names and versions.'],
  'microsoft-mail': ['Outlook', 'Read up to five recent message subjects.'],
  'microsoft-files': ['OneDrive', 'Read up to five drive item names.'],
  'google-mail': ['Gmail', 'Read up to five recent message subjects.'],
  'google-drive': ['Google Drive', 'Read up to five file names.'],
  'aws-s3': ['Amazon S3', 'List up to five objects in the configured bucket.'],
  'aws-bedrock': ['Amazon Bedrock', 'List available foundation models.'],
};
const SERVICE_BLOCKS = new Set(['http', 'service', 'rss', 'mcp', 'a2a']);
const CHILD_BLOCKS = new Set(['invoke-workflow', 'for-each', 'parallel-each']);

function showOnly(...visible) {
  if (!visible.includes('author') && !$('author').hidden) persistWorkingDraft();
  for (const id of views) $(id).hidden = !visible.includes(id);
  setNavigation(false);
  const selectedNav = {guidedStarter: 'openStarters', resources: 'manageData', teamSetup: 'openTeamSetup'}[visible[0]];
  for (const nav of ['openStarters', 'openInbox', 'manageData', 'openConnections', 'openImprovements', 'openTeamSetup']) {
    if (nav === selectedNav) $(nav).setAttribute('aria-current', 'page');
    else $(nav).removeAttribute('aria-current');
  }
}
function setNavigation(expanded, restoreFocus = false) {
  $('workspaceNavigation').dataset.expanded = String(expanded);
  $('mobileNavigation').setAttribute('aria-expanded', String(expanded));
  if (expanded) $('project').focus();
  else if (restoreFocus) $('mobileNavigation').focus();
}
function permission(name) {
  return catalog?.managed ? catalog.managed.capabilities.includes(name) : true;
}
function notice(text) {
  $('notice').textContent = text ? readableText(text) : '';
  $('notice').hidden = !text;
}
async function attempt(action) {
  if (busy) return;
  busy = true;
  notice('');
  try {
    return await action();
  } catch (error) {
    notice(error.message);
  } finally {
    busy = false;
  }
}
async function api(path, data) {
  const headers = {...(key ? {Authorization: 'Bearer ' + key} : {}),
    ...(managed.csrf ? {'X-CSRF-Token': managed.csrf} : {})};
  const options = {headers, cache: 'no-store', credentials: 'same-origin'};
  if (data !== undefined) {
    Object.assign(options, {method: 'POST', body: JSON.stringify(data),
      headers: {...headers, 'Content-Type': 'application/json'}});
  }
  const response = await fetch('/api/' + path, options);
  const result = await response.json();
  if (!response.ok) throw Error(result.error || 'Request failed.');
  return result;
}

const receiptReplay = mountReceiptReplay($('result'), api);

const panel = projectPanel({
  api, attempt, el, button, openRun, field, inputEditor, valueView,
  project: () => projectId, versions: () => catalog.versions, services: () => catalog.serviceProtocols,
  permission, version: (workflow, version) => api('version', {workflow, version}),
  draft: (plan, sample) => author(null, plan, sample),
});
const starter = mountStarterStudio($('guidedStarter'), {
  request: api, project: () => projectId, attempt, openJob: brief => author(null, null, undefined, {job: true, brief}),
  openTable: async (effect, run) => {
    returnToReceipt = run.id;
    $('closeResources').textContent = 'Back to run';
    showOnly('resources');
    await panel.open(effect.id);
  },
  fileActionLabel: () => managed.user?.method === 'desktop' ? 'Save a copy to Downloads' : 'Download saved copy',
  openFile: async (effect, run) => {
    if (managed.user?.method === 'desktop') {
      const saved = await api('desktop/save-brief-copy', {project: run.project, run: run.id});
      notice('Saved ' + saved.name + ' to your Downloads folder.');
      return;
    }
    const file = await api('resource', {project: projectId, action: 'file-read', data: {id: effect.id}});
    const url = URL.createObjectURL(new Blob([file.text], {type: 'text/markdown;charset=utf-8'}));
    const link = el('a');
    link.href = url;
    link.download = effect.name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
});
const lifecycle = mountWorkflowLifecycle($('workflowLifecycle'), {
  request: api, attempt, project: () => projectId, permission,
  onChanged: receipt => openVersion(receipt.workflow, receipt.activeVersion ?? current.version),
});

const inboxAlerts = watchInbox({
  request:api, open:openResult, project:()=>projectId, attempt,
  count:$('inboxCount'), popup:$('inboxAlert'),
  activeReference:()=>{
    const run = $('guidedStarter').hidden ? null : $('guidedStarter').querySelector('[data-run-id]')?.dataset.runId;
    return run ? 'starter:'+run : !$('work').hidden && trace?.id ? 'run:'+trace.id : null;
  },
});

function item(text, fn, active) {
  return button(text, fn, 'item' + (active ? ' on' : ''));
}

async function refresh() {
  catalog = projectId ? await api('project', {project: projectId}) : await api('workspace');
  projectId = catalog.project;
  inboxAlerts.select(projectId);
  $('project').replaceChildren(...catalog.projects.map(p => new Option(p.name, p.id, false, p.id === projectId)));
  $('library').replaceChildren(...catalog.versions.map(v => item(v.name + ' · v' + v.version +
    (v.archived ? ' · Archived' : v.activeVersion === v.version ? ' · Active release' : ''),
    () => openVersion(v.workflow, v.version), current?.workflow === v.workflow && current.version === v.version)));
  const names = new Map(catalog.versions.map(v => [v.workflow + '/' + v.version, v.name]));
  $('history').replaceChildren(...catalog.runs.map(r => {
    const title = names.get(r.workflow + '/' + r.version) || 'Workflow run';
    return item(title + ' · ' + stateLabel(r.state), () => openRun(r.id), trace?.id === r.id);
  }));
  $('draftAssistant').hidden = !permission('edit');
  $('triggerSetup').hidden = !permission('owner');
  $('addProject').hidden = !permission('owner');
  managed.renderProject(catalog);
  teamSetup.setAvailable(Boolean(key) && permission('owner') && window.isSecureContext && managed.user?.method !== 'desktop');
  drawDraftList();
}

async function unlock() {
  recoveryIdentity = null;
  await refresh();
  recoveryIdentity = await signedInDraftIdentity();
  drawDraftList();
  if (key) sessionStorage.setItem('penstock.key', key);
  $('login').hidden = true;
  $('studio').hidden = false;
  $('lock').hidden = false;
  $('mobileNavigation').hidden = false;
  $('owner').value = '';
  const reference = initialLink.get('notice');
  const requestedProject = initialLink.get('project') || (reference ? null : rememberedProject());
  if (requestedProject && catalog.projects.some(item => item.id === requestedProject)) {
    projectId = requestedProject;
    await refresh();
  }
  rememberProject();
  if (reference && (!requestedProject || requestedProject === projectId)) {
    await openResult(reference);
    return;
  }
  const recoverable = draftCopies().find(copy => draftRecoveryStatus(copy, catalog.versions) === 'current');
  if (recoverable) {
    try {
      await restoreDraft(recoverable, {refreshProject: false});
      return;
    } catch {
      notice('This local draft could not be reopened. Review its entry in Working drafts on this device.');
    }
  }
  await openStarters();
}

async function openStarters() {
  showOnly('guidedStarter');
  await starter.open();
}
async function openResult(reference) {
  const [prefix, value] = reference.split(':', 2);
  if (prefix === 'starter' && value) {
    showOnly('guidedStarter');
    await starter.openRun(value);
  } else if (prefix === 'run' && value) {
    const [id, step] = value.split('/', 2);
    await openRun(id, step);
  } else {
    throw Error('This notification does not point to a saved run.');
  }
}
async function openOperations(section, {draftContext = false} = {}) {
  showOnly('connectionsHub');
  $({connections: 'openConnections', inbox: 'openInbox', improvements: 'openImprovements'}[section])
    .setAttribute('aria-current', 'page');
  await mountConnections($('connectionsHub'), {project: projectId, request: api, admin: permission('admin'),
    canTest: permission('test'), section, openResult, openDraft: !draftContext && permission('edit') ? openDraft : null,
    useItems: !draftContext && permission('edit') ? items => author(null, {
      name: 'Review connected items',
      steps: [
        {id: 'start', type: 'start', label: 'Receive the selected items', next: 'review'},
        {id: 'review', type: 'review', label: 'Review the selected items',
          config: {question: 'Keep these items in the run result?'}, next: {approved: 'keep', declined: 'hold'}},
        {id: 'keep', type: 'finish', label: 'Items reviewed'},
        {id: 'hold', type: 'finish', label: 'Held by reviewer'},
      ],
    }, items) : null});
}

function stateLabel(state) {
  return ({waiting: 'Awaiting review', finished: 'Finished', declined: 'Declined', failed: 'Failed',
    scheduled: 'Scheduled', running: 'Running', interrupted: 'Interrupted'})[state] || labelFor(state);
}
function modeLabel(mode) {
  return ({offline: 'Offline preview', test: 'Test', live: 'Live run'})[mode] || labelFor(mode);
}
function planHas(plan, accepts) {
  return plan.steps.some(step => accepts(step) || (step.config?.body && planHas(step.config.body, accepts)));
}
function configuredModel(plan) {
  return planHas(plan, step => step.config?.provider || CHILD_BLOCKS.has(step.type));
}
function modelNames(plan, names = new Set()) {
  for (const step of plan.steps) {
    if (step.config?.provider) names.add(step.config.provider);
    if (step.config?.body) modelNames(step.config.body, names);
  }
  return names;
}
function stepConnection(step) {
  const cfg = step.config || {};
  if (Object.hasOwn(LOOKUP_BLOCKS, step.type)) {
    const profile = catalog.lookupAdapters?.[cfg.adapter];
    const operation = LOOKUP_BLOCKS[step.type].operations.find(([id]) => id === cfg.operation)?.[1] || 'Look up records';
    return {source: cfg.adapter, operation, destination: profile?.host || 'Adapter unavailable'};
  }
  if (step.type === PROJECT_READ) {
    const choice = cfg.source;
    const [source, operation] = CONNECTED_SOURCE[choice] || [labelFor(choice || 'Connected service'), 'Read from the project connection.'];
    return {source, operation, destination: 'This workflow run'};
  }
  if (!SERVICE_BLOCKS.has(step.type) || !cfg.service) return null;
  const method = catalog.serviceCapabilities?.[cfg.service]?.method;
  if (method === 'GET' || step.type === 'rss' ||
      step.type === 'mcp' && ['discover', 'list'].includes(cfg.operation) ||
      step.type === 'a2a' && ['a2a_get_agent_card', 'a2a_get_task'].includes(cfg.operation)) {
    return {source: cfg.service, operation: 'Read from the configured service.', destination: 'This workflow run'};
  }
  if (step.type === 'http' && !method) return {
    source: cfg.service, operation: 'Use the configured HTTP method. A test previews this call.',
    destination: 'Configured service or this run, depending on the method',
  };
  return {source: 'This workflow step', operation: 'Send the step data after approval.', destination: cfg.service};
}
function setRunInputs(value) {
  const start = current?.graph.nodes.find(node => node.type === 'start');
  runInput = inputEditor(value, start?.config.inputFormat || []);
  $('runInput').replaceChildren(runInput.node);
  for (const id of ['runModelConsent', 'runReadConsent', 'runWriteConsent']) $(id).checked = false;
  const model = configuredModel(current.plan);
  const service = permission('owner') && planHas(current.plan, step => SERVICE_BLOCKS.has(step.type) || CHILD_BLOCKS.has(step.type));
  const projectRead = permission('live') && planHas(current.plan, step => step.type === PROJECT_READ ||
    Object.hasOwn(LOOKUP_BLOCKS, step.type) || CHILD_BLOCKS.has(step.type));
  $('runModelLabel').hidden = !model;
  $('runReadLabel').hidden = !service && !projectRead;
  $('runWriteLabel').hidden = !service;
  $('runConsent').hidden = !model && !service && !projectRead;
  $('runConsent').open = false;
  $('runModelDisclosures').replaceChildren();
  $('test').textContent = 'Test offline';
}
async function openVersion(workflow, version, input) {
  current = await api('version', {workflow, version});
  selected = current.graph.nodes[0].id;
  trace = null;
  pendingStart = null;
  showOnly('work');
  setRunInputs(input !== undefined ? input : current.name === 'Review an intake request' ? 'Urgent request for review' : '');
  await refresh();
  render();
  $('name').focus({preventScroll: true});
}
async function openRun(id, step) {
  const run = await api('run', {id});
  await openVersion(run.workflow, run.version, run.input);
  trace = run;
  selected = current.graph.nodes.some(node => node.id === step) ? step : run.cursor || run.history.at(-1)?.node || selected;
  render();
}
function render() {
  const revision = current.archived ? 'Archived' : current.published
    ? current.activeVersion === current.version ? 'Published · Active release' : 'Published' : 'Draft';
  $('revision').textContent = 'Version ' + current.version + ' · ' + revision;
  $('name').textContent = current.name;
  $('edit').disabled = $('test').disabled = !!current.archived;
  $('run').disabled = !!current.archived || !!current.livePaused || current.activeVersion !== current.version;
  const passed = run => run.workflow === current.workflow && run.version === current.version &&
    run.state === 'finished' && ['offline', 'test'].includes(run.mode);
  $('publish').disabled = !!current.archived || !!current.published || !catalog.runs.some(passed) && !(trace && passed(trace));
  drawGraph();
  drawReceipt();
  lifecycle.show(current);
}
function drawGraph(focusSelected = false) {
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
  let activeButton;
  for (const n of nodes) {
    const b = button('',() => {selected=n.id;drawGraph(true);},'node');
    b.style.left=box.get(n.id).left+'px'; b.style.top=box.get(n.id).top+'px';
    b.title = n.label || n.id;
    b.setAttribute('aria-pressed', String(selected === n.id));
    if (selected === n.id) activeButton = b;
    b.classList.toggle('visited',receipts.has(n.id)); b.classList.toggle('active',selected===n.id);
    b.classList.toggle('waiting',trace?.state==='waiting' && trace.cursor===n.id);
    b.append(el('small',labelFor(n.type)),el('strong',n.label || n.id));canvas.append(b);
  }
  if (focusSelected === true) activeButton?.focus({preventScroll: true});
  const n=nodes.find(n=>n.id===selected) || nodes[0];
  const detail=$('inspector');
  detail.replaceChildren(el('h2', n.label || labelFor(n.type)), el('p', labelFor(n.type)));
  const connection = stepConnection(n);
  if (connection) detail.append(valueView(connection));
  const configuration = Object.fromEntries(Object.entries(n.config).filter(([field]) =>
    !['code', ...(connection ? ['service', 'source', 'operation', 'binding'] : [])].includes(field)));
  if (Object.keys(configuration).length) detail.append(valueView(configuration));
  const step = receipts.get(n.id);
  if (step) {
    const evidence = el('section', null, 'studio-step-evidence');
    evidence.append(el('h3', 'Selected step result'));
    if (step.error) evidence.append(el('p', readableText(step.error), 'guided-error'));
    if (step.decision) evidence.append(el('p', labelFor(step.decision)));
    if ('output' in step) evidence.append(resultView(step.output));
    detail.append(evidence);
  }
  refreshDiagnosisSelection(n);
}

function drawReceipt() {
  $('receipt').hidden = !trace;
  receiptReplay.show(trace, current, projectId, permission('edit'));
  if (!trace) return;
  $('status').textContent = modeLabel(trace.mode) + ' · ' + stateLabel(trace.state);
  const pending = trace.context.pending;
  const decision = $('decision');
  decision.replaceChildren();
  if (trace.state === 'waiting' && pending) {
    const node = current.graph.nodes.find(step => step.id === pending.node);
    const connection = node && stepConnection(node);
    if (connection && node.type === PROJECT_READ) {
      const question = trace.mode === 'live'
        ? 'Read from ' + connection.source + ' into this project?'
        : 'Use sample items for ' + connection.source + ' in this preview?';
      decision.append(el('strong', question), valueView(connection));
    } else if (connection && Object.hasOwn(LOOKUP_BLOCKS, node.type)) {
      decision.append(el('strong', pending.question), valueView({...connection, 'Lookup input': pending.value.query,
        'Maximum results': pending.value.limit}));
    } else {
      decision.append(el('strong', pending.question), resultView(pending.value));
    }
    const actions = el('div', null, 'actions');
    let answer;
    const reviewable = permission('review') && !current.archived && !(trace.mode === 'live' && current.livePaused);
    if (pending.answerRequired && reviewable) {
      answer = valueEditor('', {name: 'Answer'});
      decision.append(el('p', 'Answer'), answer.node);
    }
    if (pending.operation) decision.append(valueView(pending.operation));
    if (reviewable) {
      for (const [label, approve] of [['Approve', true], ['Decline', false]]) {
        actions.append(button(label, async () => {
          const updated = await api('decide', {id: trace.id, node: pending.node, approve,
            answer: answer && approve ? answer.read() : undefined});
          await openRun(updated.id);
        }));
      }
    } else {
      decision.append(el('p', current.archived ? 'Recover this workflow to make a decision.'
        : current.livePaused && trace.mode === 'live' ? 'Resume live runs to make a decision.'
          : 'A project reviewer can resolve this decision.'));
    }
    decision.append(actions);
  }
  if (trace.context.childRuns) {
    for (const id of trace.context.childRuns) {
      decision.append(button('Open child run', () => openRun(id)));
    }
  }
  $('refreshReceipt').onclick = () => attempt(() => openRun(trace.id));
  $('steps').replaceChildren(...trace.history.map(step => {
    const node = current.graph.nodes.find(n => n.id === step.node);
    const row = el('li');
    const label = node?.label || labelFor(node?.type || step.node);
    row.append(button(label, () => {selected = step.node; drawGraph();}, 'studio-quiet'));
    row.append(el('span', ' · ' + (step.error ? readableText(step.error) : labelFor(step.decision || step.port || 'finished')) +
      (step.preview ? ' · Delivery preview' : ''), step.error ? 'guided-error' : ''));
    return row;
  }));
  const result = $('result');
  result.replaceChildren();
  if (trace.state === 'finished') {
    result.append(el('h3', 'Result'), resultView(trace.context.result));
  } else if (trace.state === 'interrupted') {
    result.append(el('p', 'The server stopped during this run. Review the step history and check any external changes before starting again.'));
  } else if (trace.state === 'failed') {
    const failure = trace.history.at(-1);
    result.append(el('h3', 'What needs attention'),
      el('p', failure?.error ? readableText(failure.error) : 'This workflow did not finish.', 'guided-error'),
      el('p', 'Review the failed step and adjust its inputs or settings before testing again.'));
  }
}

let draftInput, authorMode, draftProviders = {}, diagnosisKey;
let recoveryIdentity, workingDraft, persistTimer;
const draftingProviders = async () => (await api('drafting-providers', {project: projectId})).providers;
const authorTools = el('section', null, 'actions');
const recoveryFeedback = el('p');
recoveryFeedback.setAttribute('role', 'status');
$('authorTitle').parentNode.after(authorTools, recoveryFeedback);
const draftList = el('details');
$('library').after(draftList);
draftList.hidden = true;

async function signedInDraftIdentity() {
  if (!key) return managed.user?.id ? 'account:' + managed.user.id : null;
  if (!crypto.subtle) return null;
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(key));
  return 'owner:' + Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

function projectSelectionKey() {
  return recoveryIdentity && 'penstock.last-project.v1/' + encodeURIComponent(recoveryIdentity);
}

function rememberedProject() {
  const name = projectSelectionKey();
  return name ? sessionStorage.getItem(name) : null;
}

function rememberProject() {
  const name = projectSelectionKey();
  if (name) sessionStorage.setItem(name, projectId);
}

function recoveryScope() {
  if (!recoveryIdentity || !projectId || !permission('edit')) return null;
  try {
    const role = catalog.managed?.role || 'owner';
    return {identity: recoveryIdentity, project: projectId, role,
      store: createDraftRecovery(window.localStorage, recoveryIdentity, projectId, role)};
  } catch {
    return null;
  }
}

function sameDraftScope(left, right) {
  return left && right && left.identity === right.identity && left.project === right.project && left.role === right.role;
}

function draftCopies() {
  try { return recoveryScope()?.store.list() || []; }
  catch { return []; }
}

function draftTitle(copy) {
  return copy.editor?.name?.trim() || copy.title || 'Untitled workflow';
}

function drawDraftList() {
  const drafts = draftCopies();
  draftList.hidden = !drafts.length;
  draftList.replaceChildren(el('summary', 'Working drafts on this device'));
  for (const copy of drafts) {
    const row = el('div');
    row.append(el('p', draftTitle(copy)));
    const status = draftRecoveryStatus(copy, catalog.versions);
    const description = !copy.workflow ? 'Unsaved new workflow' : status === 'current'
      ? 'Unsaved edits based on version ' + copy.baseVersion : status === 'newer'
        ? 'Newer saved version available' : 'Archived or unavailable saved version';
    row.append(el('small', description));
    row.append(button(status === 'current' ? 'Restore working draft' : 'Recover as a new workflow',
      () => restoreDraft(copy, {asNew: status !== 'current'}), 'studio-quiet'));
    const latest = catalog.versions.filter(version => version.workflow === copy.workflow)
      .sort((left, right) => right.version - left.version)[0];
    if (latest) row.append(button('Open saved version ' + latest.version,
      () => openVersion(latest.workflow, latest.version), 'studio-quiet'));
    row.append(button('Discard working copy', () => confirmDiscard(row, () => {
      recoveryScope()?.store.discard(copy.id);
      if (workingDraft?.id === copy.id && sameDraftScope(workingDraft.scope, recoveryScope())) {
        workingDraft.remember = false;
        renderAuthorTools();
      }
      drawDraftList();
    }), 'studio-quiet'));
    draftList.append(row);
  }
}

function confirmDiscard(container, discard) {
  if (container.querySelector('[data-discard-confirmation]')) return;
  const confirm = el('div', null, 'actions');
  confirm.dataset.discardConfirmation = '';
  confirm.append(el('p', 'Discard the unsaved working copy?'),
    button('Discard', discard, 'studio-quiet'),
    button('Keep editing', () => confirm.remove(), 'studio-quiet'));
  container.append(confirm);
  confirm.querySelector('button')?.focus();
}

function persistWorkingDraft() {
  clearTimeout(persistTimer);
  if (!workingDraft?.remember || !sameDraftScope(workingDraft.scope, recoveryScope())) return;
  try {
    workingDraft.scope.store.write({
      id: workingDraft.id, at: Date.now(), workflow: editing?.workflow || null,
      baseVersion: editing?.version || null,
      title: editing?.name || (authorMode?.job ? 'Job plan' : 'New workflow'),
      job: !!authorMode?.job, summary: authorMode?.summary,
      editor: editor?.snapshot() || null, brief: $('brief').value,
      sample: draftInput, provider: $('provider').value, assistantOpen: $('draftAssistant').open,
      sourceId: workingDraft.sourceId,
    });
  } catch (error) {
    recoveryFeedback.textContent = error.message || 'This browser could not store the working draft.';
  }
}

function scheduleDraftRecovery() {
  clearTimeout(persistTimer);
  if (workingDraft?.remember) persistTimer = setTimeout(persistWorkingDraft, 150);
}

function renderAuthorTools() {
  authorTools.replaceChildren();
  recoveryFeedback.textContent = '';
  const preserve = el('input');
  preserve.type = 'checkbox';
  preserve.checked = !!workingDraft?.remember;
  preserve.disabled = !workingDraft?.scope;
  authorTools.append(field('Keep this draft on this device', preserve),
    button('Set up connections', visitDraftConnections, 'studio-quiet'));
  if (editing) authorTools.append(button('Open saved version ' + editing.version,
    () => openVersion(editing.workflow, editing.version), 'studio-quiet'));
  authorTools.append(button('Discard working draft', () => confirmDiscard(authorTools, discardWorkingDraft), 'studio-quiet'));
  const disclosure = el('p', preserve.disabled
    ? 'Device recovery is unavailable in this browser session.'
    : 'Stores the job brief, workflow edits, and draft test inputs in this browser profile until you save, discard, or sign out. Other people using this browser profile may be able to access the copy.');
  authorTools.append(disclosure);
  preserve.onchange = () => {
    if (!sameDraftScope(workingDraft?.scope, recoveryScope())) return;
    workingDraft.remember = preserve.checked;
    if (preserve.checked) persistWorkingDraft();
    else workingDraft.scope.store.discard(workingDraft.id);
    drawDraftList();
  };
}

async function discardWorkingDraft() {
  const saved = editing;
  clearTimeout(persistTimer);
  workingDraft?.scope?.store.discard(workingDraft.id);
  workingDraft = null;
  editor = null;
  $('proposal').replaceChildren();
  $('brief').value = '';
  drawDraftList();
  if (saved) await openVersion(saved.workflow, saved.version);
  else await openStarters();
}

async function restoreDraft(copy, {refreshProject = true, asNew = false} = {}) {
  if (refreshProject) await refresh();
  const scope = recoveryScope();
  const currentCopy = scope?.store.list().find(entry => entry.id === copy.id);
  if (!currentCopy) {
    throw Error('This working draft is unavailable for your current project role.');
  }
  copy = currentCopy;
  if (!asNew && draftRecoveryStatus(copy, catalog.versions) !== 'current') {
    throw Error('A newer version is saved. Open it or recover the working copy as a new workflow.');
  }
  const saved = copy.workflow && !asNew ? await api('version', {workflow: copy.workflow, version: copy.baseVersion}) : null;
  await author(saved, copy.editor?.plan, copy.sample, {job: !!copy.job, brief: copy.brief, summary: copy.summary,
    recovered: copy, asNew});
  recoveryFeedback.textContent = 'Working draft recovered.';
}

async function visitDraftConnections() {
  const context = workingDraft;
  if (!context) return;
  resetDraftConsent();
  context.provider = $('provider').value;
  persistWorkingDraft();
  await openOperations('connections', {draftContext: true});
  const returnBar = el('div', null, 'actions');
  const back = button('Return to this draft', async () => {
    await refresh();
    if (workingDraft !== context || context.project !== projectId || context.identity !== recoveryIdentity ||
        context.role !== (catalog.managed?.role || 'owner') || !permission('edit')) {
      throw Error('This draft belongs to a different project or role.');
    }
    showOnly('author');
    resetDraftConsent();
    $('authorTitle').focus({preventScroll: true});
    await loadJobProviders(context.provider);
    persistWorkingDraft();
    if (editing && draftRecoveryStatus({workflow: editing.workflow, baseVersion: editing.version}, catalog.versions) !== 'current') {
      recoveryFeedback.textContent = 'A newer version is saved. Open it or recover your working copy as a new workflow.';
    }
  }, 'primary');
  returnBar.append(back);
  if (!context.remember && context.scope) {
    const keep = button('Keep draft for sign-in', () => {
      if (!sameDraftScope(context.scope, recoveryScope())) throw Error('Your project role has changed.');
      context.remember = true;
      persistWorkingDraft();
      drawDraftList();
      renderAuthorTools();
      keep.disabled = true;
    }, 'studio-quiet');
    returnBar.append(keep, el('p', 'To recover after a provider sign-in reloads this tab, save the brief, edits, and draft inputs in this browser profile.'));
  }
  $('connectionsHub').prepend(returnBar);
  back.focus();
}

function describeProvider(name, profile) {
  if (!profile) return null;
  const details = el('dl', null, 'studio-model-disclosure');
  for (const [title, value] of [
    ['Model', profile.model],
    ['Destination', name + ' · ' + profile.host + (profile.local ? ' · Local' : '')],
    ['Cost', profile.cost],
  ]) {
    const line = el('div');
    line.append(el('dt', title), el('dd', value));
    details.append(line);
  }
  return details;
}

function showPlanOverview(plan, summary) {
  const overview = $('jobPlanOverview');
  overview.hidden = !plan || (!authorMode?.job && !summary);
  overview.replaceChildren();
  if (overview.hidden) return;
  overview.append(el('h2', summary ? 'Proposed repair' : 'Proposed job plan'),
    el('p', summary || 'Review the steps, change their settings if needed, then save a draft to test.'));
  const connections = plan.steps.map(stepConnection).filter(Boolean);
  if (connections.length) {
    overview.append(el('h3', 'Planned connections'));
    const reads = el('ul');
    for (const item of connections) reads.append(el('li', item.source + ' · ' + item.operation + ' Destination: ' + item.destination));
    overview.append(reads);
  }
}

function setEditor(plan, options = {}) {
  editor = plan ? planEditor(plan, attempt, {step: options.node, recovered: options.recovered?.editor,
    lookupAdapters: catalog.lookupAdapters}) : null;
  $('proposal').replaceChildren(...(editor ? [editor.node] : []));
  $('save').hidden = !editor;
  showPlanOverview(plan, options.summary);
}
async function author(version, proposal, sample, options = {}) {
  if (!permission('edit')) throw Error('This project role can view workflows. Ask a project editor to make changes.');
  if (version?.archived) throw Error('Recover this workflow before editing it.');
  persistWorkingDraft();
  const scope = recoveryScope();
  workingDraft = {
    id: options.recovered && !options.asNew ? options.recovered.id : requestId(),
    sourceId: options.asNew ? options.recovered?.id : options.recovered?.sourceId || null,
    scope, remember: !!options.recovered && !!scope,
    identity: recoveryIdentity, project: projectId, role: catalog.managed?.role || 'owner',
  };
  editing = version;
  authorMode = options;
  draftInput = sample;
  const plan = proposal || version?.plan || (options.job ? null : await (await fetch('/examples/review.json')).json());
  setEditor(plan, options);
  $('authorTitle').textContent = options.job ? 'Plan your job' : 'Draft a workflow';
  $('brief').value = options.brief || '';
  $('draftConsent').checked = false;
  $('includeResources').checked = false;
  $('draftAssistant').open = options.job === true || !!options.recovered?.assistantOpen;
  $('manualJob').hidden = !!plan;
  $('manualJob').disabled = options.job === true;
  $('offline').hidden = true;
  $('providerSetup').hidden = true;
  draftProviders = {};
  $('provider').replaceChildren();
  updateDraftConsent();
  renderAuthorTools();
  showOnly('author');
  $('authorTitle').focus({preventScroll: true});
  if ($('draftAssistant').open) {
    try { await loadJobProviders(options.recovered?.provider); }
    finally { $('manualJob').disabled = false; }
  }
  if (options.asNew && workingDraft.remember) persistWorkingDraft();
}
async function openDraft({plan, workflow, version, node, summary, sample}) {
  if (!workflow) return author(null, plan, sample, {node, summary});
  current = await api('version', {workflow, version});
  trace = null;
  selected = node || current.graph.nodes[0].id;
  setRunInputs(sample ?? '');
  render();
  return author(current, plan, sample, {node, summary});
}
async function loadJobProviders(selection = $('provider').value) {
  draftProviders = await draftingProviders();
  const entries = Object.entries(draftProviders);
  $('provider').replaceChildren(...entries.map(([name, profile]) =>
    new Option(name + ' · ' + profile.model, name, false, name === selection)));
  $('providerSetup').hidden = !entries.length;
  $('offline').hidden = !!entries.length;
  resetDraftConsent();
}
function updateDraftConsent() {
  const name = $('provider').value;
  const profile = draftProviders[name];
  $('generate').disabled = !(profile && $('brief').value.trim() && $('draftConsent').checked);
  const disclosure = $('providerDisclosure');
  if (!profile) {
    disclosure.replaceChildren();
    return;
  }
  const scope = $('includeResources').checked
    ? 'This request includes your brief, available workflow tools, and the selected project context.'
    : 'This request includes your brief and the available workflow tools.';
  disclosure.replaceChildren(describeProvider(name, profile), el('p', scope));
  $('draftConsentText').textContent = 'Send this drafting request to ' + name + ' at ' + profile.host;
}
function resetDraftConsent() {
  $('draftConsent').checked = false;
  updateDraftConsent();
}

function refreshDiagnosisSelection(node) {
  const context = {project: projectId, workflow: current.workflow, version: current.version,
    run: trace?.id, node: node.id};
  const key = JSON.stringify(context);
  if (key === diagnosisKey) return;
  diagnosisKey = key;
  const details = $('diagnosis');
  const content = $('diagnosisContent');
  content.replaceChildren(el('p', (node.label || labelFor(node.type)) + ' · Version ' + current.version +
    (trace ? ' · Selected run' : ' · Workflow settings')));
  content.append(button('Check selected step', async () => {
    const finding = await api('diagnose', context);
    if (key === diagnosisKey) renderDiagnosis(content, finding, context);
  }, 'studio-quiet'));
  content.append(el('p', 'Checks the selected workflow version and receipt locally.'));
  if (!details.open) return;
  details.querySelector('summary').textContent = 'Diagnose selected step';
}

function renderDiagnosis(content, finding, context) {
  const guidance = el('ol');
  guidance.append(...finding.guidance.map(line => el('li', line)));
  content.replaceChildren(el('h3', finding.title), el('p', finding.step + ' · Version ' + finding.version), guidance);
  if (!finding.canEdit || current.archived) return;
  const repair = finding.repair || {workflow: finding.workflow, version: finding.version, node: finding.node};
  content.append(button(finding.repair ? 'Review suggested repair' : 'Edit this step in a draft',
    () => openDraft({...repair, sample: trace?.input}), 'studio-quiet'));
  if (!Object.keys(catalog.providers).length) return;
  const assist = el('details', null, 'studio-diagnosis-assist');
  assist.append(el('summary', 'Ask a model for more guidance'));
  assist.ontoggle = () => {
    if (assist.open && !assist.dataset.loaded) attempt(async () => {
      const profiles = await draftingProviders();
      assist.dataset.loaded = 'true';
      const entries = Object.entries(profiles);
      if (!entries.length) {
        assist.append(el('p', 'No model providers are configured.'));
        return;
      }
      const select = selectInput(entries.map(([name, profile]) => [name, name + ' · ' + profile.model]), entries[0][0]);
      const consent = el('input');
      consent.type = 'checkbox';
      const label = el('span');
      const check = el('label');
      check.append(consent, ' ', label);
      const disclosure = el('div');
      const result = el('div');
      result.setAttribute('aria-live', 'polite');
      const ask = button('Get model guidance', async () => {
        const name = select.value;
        const response = await api('diagnose', {...context, provider: name, modelConsent: consent.checked,
          providerBinding: profiles[name].binding});
        consent.checked = false;
        ask.disabled = true;
        result.replaceChildren(el('h3', 'Model guidance'), resultView(response.assistant.text));
      }, 'studio-quiet');
      function showModel() {
        const name = select.value;
        const profile = profiles[name];
        disclosure.replaceChildren(describeProvider(name, profile));
        label.textContent = 'Send the failure category, step type, and run state to ' + name + ' at ' + profile.host;
        consent.checked = false;
        ask.disabled = true;
      }
      select.onchange = showModel;
      consent.onchange = () => {ask.disabled = !consent.checked;};
      showModel();
      assist.append(field('Diagnosis provider', select), disclosure, check, ask, result);
    });
  };
  content.append(assist);
}

async function start(mode) {
  const usesModel = configuredModel(current.plan);
  const consent = {
    modelConsent: usesModel && $('runModelConsent').checked,
    liveReadConsent: mode === 'live' && $('runReadConsent').checked,
    liveWriteConsent: mode === 'live' && $('runWriteConsent').checked,
  };
  const fields = {workflow: current.workflow, version: current.version,
    mode: mode === 'live' ? 'live' : consent.modelConsent ? 'test' : 'offline',
    input: runInput.read(), ...consent, ...(mode === 'live' ? {activeRelease: true} : {})};
  const intent = JSON.stringify(fields);
  if (pendingStart?.intent !== intent) pendingStart = {intent, id: requestId()};
  const result = await api('start', {...fields, request: pendingStart.id});
  await openRun(result.id);
}
async function changeProject(id) {
  persistWorkingDraft();
  workingDraft = null;
  editor = null;
  $('brief').value = '';
  $('proposal').replaceChildren();
  projectId = id;
  current = trace = editing = pendingStart = null;
  lifecycle.clear();
  $('newProjectForm').hidden = true;
  await refresh();
  rememberProject();
  await openStarters();
}

$('unlock').onsubmit = event => {
  event.preventDefault();
  key = $('owner').value.trim();
  attempt(unlock);
};
$('lock').onclick = () => attempt(async () => {
  if (!key && managed.user) await managed.logout();
  lifecycle.clear();
  clearTimeout(persistTimer);
  if (recoveryIdentity) {
    try { createDraftRecovery(window.localStorage, recoveryIdentity, projectId, '').clearAccount(); }
    catch { /* Browser storage may be disabled for this origin. */ }
  }
  workingDraft = null;
  const selection = projectSelectionKey();
  if (selection) sessionStorage.removeItem(selection);
  sessionStorage.removeItem('penstock.key');
  location.reload();
});
$('mobileNavigation').onclick = () => {
  const next = $('mobileNavigation').getAttribute('aria-expanded') !== 'true';
  setNavigation(next, !next);
};
$('workspaceNavigation').addEventListener('keydown', event => {
  if (event.key === 'Escape' && $('mobileNavigation').getAttribute('aria-expanded') === 'true') {
    setNavigation(false, true);
  }
});
$('openStarters').onclick = () => attempt(openStarters);
$('openInbox').onclick = () => attempt(() => openOperations('inbox'));
$('openConnections').onclick = () => attempt(() => !$('author').hidden && workingDraft
  ? visitDraftConnections() : openOperations('connections'));
$('openImprovements').onclick = () => attempt(() => openOperations('improvements'));
$('new').onclick = () => attempt(() => author(null));
$('edit').onclick = () => attempt(() => author(current));
$('cancel').onclick = () => attempt(() => current && !authorMode?.job ? showOnly('work') : openStarters());
$('draftAssistant').ontoggle = () => {
  if ($('draftAssistant').open && !authorMode?.job) attempt(loadJobProviders);
};
$('brief').oninput = $('provider').onchange = $('includeResources').onchange = () => {
  resetDraftConsent();
  scheduleDraftRecovery();
};
$('proposal').addEventListener('input', scheduleDraftRecovery);
$('proposal').addEventListener('change', scheduleDraftRecovery);
$('proposal').addEventListener('click', scheduleDraftRecovery);
window.addEventListener('pagehide', persistWorkingDraft);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') persistWorkingDraft();
});
$('draftConsent').onchange = updateDraftConsent;
$('generate').onclick = () => attempt(async () => {
  const name = $('provider').value;
  const route = draftProviders[name];
  if (!route || !$('draftConsent').checked) throw Error('Review and confirm the named drafting provider.');
  const result = await api('propose', {brief: $('brief').value.trim(), includeResources: $('includeResources').checked,
    project: projectId, provider: name, modelConsent: true, providerBinding: route.binding});
  resetDraftConsent();
  setEditor(result.plan);
  $('manualJob').hidden = true;
  persistWorkingDraft();
  notice('Proposal ready to review.');
});
$('manualJob').onclick = () => attempt(() => {
  setEditor({name: 'New workflow', steps: [
    {id: 'start', type: 'start', label: 'Receive input', next: 'finish'},
    {id: 'finish', type: 'finish', label: 'Return result'},
  ]});
  $('manualJob').hidden = true;
  persistWorkingDraft();
});
$('save').onclick = () => attempt(async () => {
  const result = await api('save', {plan: editor.read(), project: projectId,
    workflow: editing?.workflow, baseVersion: editing?.version});
  clearTimeout(persistTimer);
  const saved = workingDraft;
  workingDraft = null;
  let cleanupError = false;
  if (saved?.scope) {
    try {
      saved.scope.store.discard(saved.id);
      if (saved.sourceId) saved.scope.store.discard(saved.sourceId);
    } catch {
      cleanupError = true;
    }
  }
  await openVersion(result.workflow, result.version, draftInput);
  if (cleanupError) notice('The workflow was saved. This browser could not remove the local working copy.');
});
$('layout').onchange = drawGraph;
$('runModelConsent').onchange = () => {
  $('test').textContent = $('runModelConsent').checked ? 'Run model test' : 'Test offline';
};
$('runConsent').ontoggle = () => {
  if (!$('runConsent').open || $('runModelLabel').hidden) return;
  attempt(async () => {
    const existing = $('runModelDisclosures');
    const names = [...modelNames(current.plan)];
    const providers = await draftingProviders();
    existing.replaceChildren(...names.map(name => describeProvider(name, providers[name]) ||
      el('p', name + ' is unavailable in this workspace.')));
    if (planHas(current.plan, step => CHILD_BLOCKS.has(step.type))) {
      existing.append(el('p', 'This workflow can call other published workflows. Review their model steps before enabling model calls.'));
    }
  });
};
$('publish').onclick = () => attempt(async () => {
  current = await api('publish', {workflow: current.workflow, version: current.version});
  await refresh();
  render();
});
$('test').onclick = () => attempt(() => start('test'));
$('run').onclick = () => attempt(() => start('live'));
$('project').onchange = () => attempt(() => changeProject($('project').value));
$('addProject').onclick = () => {
  $('newProjectForm').hidden = !$('newProjectForm').hidden;
  if (!$('newProjectForm').hidden) $('newProjectName').focus();
};
$('newProjectForm').onsubmit = event => {
  event.preventDefault();
  attempt(async () => {
    const saved = await api('project-create', {name: $('newProjectName').value});
    $('newProjectName').value = '';
    await changeProject(saved.id);
  });
};
$('manageData').onclick = () => attempt(async () => {
  returnToReceipt = null;
  $('closeResources').textContent = 'Back to workflow';
  showOnly('resources');
  await panel.open();
});
$('closeResources').onclick = () => attempt(() => {
  if (returnToReceipt) return openResult('starter:' + returnToReceipt);
  return current ? showOnly('work') : openStarters();
});

const managed = mountManagedAccess({api, notice, onSession: () => {
  key = '';
  return attempt(unlock);
}});
const teamSetup = mountTeamSetup({request: api, attempt, show: () => showOnly('teamSetup'),
  openRoles: () => { setNavigation(true); managed.openProjectAccess(); },
  openConnections: () => openOperations('connections'), openNotifications: () => openOperations('inbox')});
if (key) {
  await managed.restore({activate: false});
  await attempt(unlock);
} else {
  await managed.restore();
}
