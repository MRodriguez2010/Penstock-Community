import {field, inputEditor, labelFor, requestId, selectInput, textInput, valueView} from './studioFields.js';

const $ = id => document.getElementById(id);
const DAYS = [['MON', 'Monday'], ['TUE', 'Tuesday'], ['WED', 'Wednesday'], ['THU', 'Thursday'],
  ['FRI', 'Friday'], ['SAT', 'Saturday'], ['SUN', 'Sunday']];
const TRIGGER_LABEL = {schedule: 'Schedule', rss: 'Feed item', 'app-trigger': 'Service item',
  webhook: 'Webhook', 'table-event': 'Table change', 'workspace-event': 'Project change'};

function options(values, value) {
  return selectInput(values, value);
}
function typedCell(column, value = '') {
  if (column.type === 'select') return options(['', ...column.options].map(item => [item, item || 'Choose one']), value);
  if (column.type === 'boolean') return options([['', 'Choose one'], ['true', 'Yes'], ['false', 'No']], value);
  const input = textInput(value, {type: ['number', 'date', 'url', 'email'].includes(column.type) ? column.type : 'text'});
  if (column.type === 'number') input.step = 'any';
  return input;
}
function zoneName() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}

export function projectPanel({api, attempt, el, button, openRun, project, versions, services, draft, permission, version}) {
  let tables = [];
  let page, offset = 0, controls = {}, selectedProject, cell, scheduleInput;
  let payloadKey, payloadGeneration = 0;
  const request = (path, fields = {}) => api(path, {project: project(), ...fields});
  const data = (action, fields) => request('resource', {action, data: fields});
  const mutate = (action, fields) => data(action, {...fields, key: requestId()});
  const handle = (id, action) => {$(id).onclick = () => attempt(action);};

  function closeCell() {
    $('cellEdit').hidden = true;
    cell = null;
  }
  function editCell(id, row, column) {
    const previous = $('cellEditValue');
    const input = typedCell(column, row.values[column.id]);
    input.id = 'cellEditValue';
    previous.replaceWith(input);
    $('cellEditLabel').firstChild.textContent = 'Edit ' + column.name;
    $('cellEdit').hidden = false;
    cell = {id, row, column};
    input.focus();
  }
  async function showTable(id, start = 0) {
    offset = start;
    closeCell();
    page = await data('table-read', {id, offset, limit: 25});
    $('tableView').hidden = false;
    $('tableTitle').textContent = page.table.name + ' · ' + page.total + (page.total === 1 ? ' row' : ' rows');
    const table = el('table');
    const heading = el('thead');
    const columns = el('tr');
    for (const name of page.table.columns) {
      const th = el('th', name);
      th.scope = 'col';
      columns.append(th);
    }
    heading.append(columns);
    const body = el('tbody');
    for (const record of page.records) {
      const row = el('tr');
      for (const col of page.table.schema) {
        const td = el('td');
        const value = record.values[col.id];
        if (permission('edit')) td.append(button(value || 'Edit', () => editCell(id, record, col)));
        else td.textContent = value;
        row.append(td);
      }
      body.append(row);
    }
    table.append(heading, body);
    $('records').replaceChildren(table);
    $('prevPage').disabled = offset === 0;
    $('nextPage').disabled = page.nextOffset == null;
    $('rowFields').replaceChildren(...page.table.schema.map(col => {
      const box = typedCell(col);
      box.name = col.id;
      return field(col.name, box);
    }));
  }

  function timed() {
    return ['interval', 'calendar'].includes($('triggerKind').value);
  }
  function target() {
    if (!$('target').value) return null;
    const [workflow, selectedVersion] = $('target').value.split('/');
    return {workflow, version: Number(selectedVersion)};
  }
  async function showPayload() {
    const selection = target();
    const key = timed() && selection ? project() + '/' + selection.workflow + '/' + selection.version : null;
    if (key === payloadKey) return;
    payloadKey = key;
    scheduleInput = null;
    const generation = ++payloadGeneration;
    $('triggerInput').replaceChildren();
    if (!key) return;
    const workflow = await version(selection.workflow, selection.version);
    if (generation !== payloadGeneration) return;
    const fields = workflow.graph.nodes.find(node => node.type === 'start')?.config?.inputFormat || [];
    scheduleInput = inputEditor(fields.length ? {} : '', fields);
    $('triggerInput').replaceChildren(scheduleInput.node);
  }
  function calendarControls(parent) {
    const frequency = options([['daily', 'Every day'], ['weekly', 'Every week'], ['monthly', 'Every month'],
      ['hourly', 'Every hour'], ['minutes', 'Every few minutes']], controls.frequency?.value || 'daily');
    const zone = textInput(controls.zone?.value || zoneName());
    const timing = el('div', null, 'guided-fields two');
    controls.frequency = frequency;
    controls.zone = zone;
    parent.append(field('Repeat', frequency), field('Time zone', zone), timing);
    function display() {
      const mode = frequency.value;
      timing.replaceChildren();
      if (mode === 'minutes') {
        controls.minutes = textInput('15', {type: 'number'});
        controls.minutes.min = 1;
        controls.minutes.max = 60;
        timing.append(field('Minutes between checks', controls.minutes));
      } else if (mode === 'hourly') {
        controls.minute = textInput('0', {type: 'number'});
        controls.minute.min = 0;
        controls.minute.max = 59;
        timing.append(field('Minute of the hour', controls.minute));
      } else {
        if (mode === 'weekly') {
          controls.day = options(DAYS, 'MON');
          timing.append(field('Day of week', controls.day));
        }
        if (mode === 'monthly') {
          controls.day = textInput('1', {type: 'number'});
          controls.day.min = 1;
          controls.day.max = 31;
          timing.append(field('Day of month', controls.day));
        }
        controls.time = textInput('09:00', {type: 'time'});
        timing.append(field('Time', controls.time));
      }
    }
    frequency.onchange = display;
    display();
  }
  async function triggerFields() {
    const kind = $('triggerKind').value;
    controls = {};
    const fields = el('div', null, 'guided-fields');
    const add = (key, label, node) => {controls[key] = node; fields.append(field(label, node));};
    if (kind === 'interval') add('seconds', 'Interval in seconds', textInput('3600', {type: 'number'}));
    if (kind === 'calendar') calendarControls(fields);
    if (['rss', 'app-trigger'].includes(kind)) {
      const match = kind === 'rss' ? 'rss' : 'http';
      add('service', 'Registered service', options(Object.entries(services()).filter(([, protocol]) => protocol === match).map(([name]) => [name, name])));
      add('seconds', 'Poll interval in seconds', textInput('60', {type: 'number'}));
      if (kind === 'app-trigger') {
        add('path', 'List location (leave empty for a top-level list)', textInput());
        add('idField', 'Unique item field', textInput('id'));
      }
    }
    if (kind === 'table-event') {
      add('table', 'Table', options(tables.map(table => [table.id, table.name])));
      add('event', 'Change', options([['insert', 'Row added'], ['update', 'Row updated'], ['delete', 'Row deleted']]));
    }
    if (kind === 'workspace-event') add('action', 'Project change', options([
      ['table', 'Table created'], ['table-row', 'Row added'], ['file-save', 'File saved'], ['document-save', 'Document saved'],
    ]));
    $('timingFields').replaceChildren(fields);
    $('payloadLabel').hidden = $('previewSchedule').hidden = !timed();
    await showPayload();
  }
  function config() {
    switch ($('triggerKind').value) {
      case 'rss': return {service: controls.service.value, seconds: Number(controls.seconds.value)};
      case 'app-trigger': return {service: controls.service.value, seconds: Number(controls.seconds.value),
        itemsPath: controls.path.value.split('.').filter(Boolean), idField: controls.idField.value};
      case 'interval': return {seconds: Number(controls.seconds.value)};
      case 'calendar': {
        const scheduleType = controls.frequency.value;
        const timezone = controls.zone.value.trim();
        const fields = {
          minutes: () => ({minutesInterval: Number(controls.minutes.value)}),
          hourly: () => ({hourlyMinute: Number(controls.minute.value)}),
          daily: () => ({dailyTime: controls.time.value}),
          weekly: () => ({weeklyDay: controls.day.value, weeklyDayTime: controls.time.value}),
          monthly: () => ({monthlyDay: Number(controls.day.value), monthlyTime: controls.time.value}),
        };
        return {scheduleType, timezone, ...fields[scheduleType]()};
      }
      case 'table-event': return {tableId: controls.table.value, event: controls.event.value};
      case 'workspace-event': return {actions: [controls.action.value]};
      default: return {};
    }
  }
  function scheduleName(trigger) {
    if (trigger.kind !== 'schedule') return TRIGGER_LABEL[trigger.kind] || labelFor(trigger.kind);
    const timing = JSON.parse(trigger.config).timing;
    if (timing.seconds) return 'Every ' + timing.seconds + ' seconds';
    return 'Calendar · ' + timing.timezone;
  }
  async function refresh() {
    const [resources, schedules] = await Promise.all([request('resources'), request('triggers')]);
    tables = resources.tables;
    $('projectAssets').replaceChildren(...[...resources.files, ...resources.documents].map(item =>
      el('p', (item.name || item.title) + ' · Version ' + item.version)));
    $('tableList').replaceChildren(...tables.map(table => button(table.name, () => showTable(table.id))));
    if (!tables.length) $('tableList').textContent = 'No project tables yet.';
    for (const id of ['tableCreateOptions', 'columnOptions', 'intakeWorkflow', 'addRow', 'projectAssetActions']) {
      $(id).hidden = !permission('edit');
    }
    const targets = versions().filter(item => item.published);
    const selectedTarget = $('target').value;
    $('target').replaceChildren(...targets.map(item => {
      const key = item.workflow + '/' + item.version;
      return new Option(item.name + ' · Version ' + item.version, key, false, key === selectedTarget);
    }));
    $('armTrigger').disabled = !targets.length;
    if (!targets.length) $('target').add(new Option('Publish a tested workflow to enable a trigger', ''));
    $('triggers').replaceChildren(...schedules.triggers.map(trigger => {
      const workflow = versions().find(item => item.workflow === trigger.workflow && item.version === trigger.version);
      const line = el('div', null, 'studio-schedule-row');
      line.append(el('strong', workflow?.name || 'Workflow'), el('span', 'Version ' + trigger.version + ' · ' +
        scheduleName(trigger) + ' · ' + (trigger.enabled ? 'enabled' : 'paused')));
      if (trigger.next_due && trigger.enabled) line.append(el('small', 'Next run ' + new Date(trigger.next_due * 1000).toLocaleString()));
      if (trigger.last_error) line.append(el('p', trigger.last_error, 'guided-error'));
      if (permission('owner')) line.append(button(trigger.enabled ? 'Pause' : 'Resume', async () => {
        await request('trigger-enable', {id: trigger.id, enabled: !trigger.enabled});
        await refresh();
      }));
      return line;
    }));
    if (!schedules.triggers.length) $('triggers').textContent = 'No triggers yet.';
    $('deliveries').replaceChildren(...schedules.deliveries.map(delivery => delivery.run
      ? button('Open run · ' + labelFor(delivery.state), () => openRun(delivery.run))
      : el('p', labelFor(delivery.state) + (delivery.detail ? ' · ' + delivery.detail : ''))));
    if (!schedules.deliveries.length) $('deliveries').textContent = 'No deliveries yet.';
    if (selectedProject !== project()) {
      page = null;
      selectedProject = project();
      $('tableView').hidden = true;
      closeCell();
    }
    if (permission('owner')) await triggerFields();
  }

  handle('intakeWorkflow', async () => {
    const table = page.table;
    const plan = {name: 'Review rows for ' + table.name, steps: [
      {id: 'start', type: 'start', label: 'Receive a row', next: 'review'},
      {id: 'review', type: 'review', config: {question: 'Add this row to ' + table.name + '?'}, next: {approved: 'append', declined: 'hold'}},
      {id: 'append', type: 'table', config: {operation: 'insert_row', tableId: table.id, data: {ref: {node: '$input'}}}, next: 'saved'},
      {id: 'saved', type: 'finish', label: 'Row saved'}, {id: 'hold', type: 'finish', label: 'Row declined'},
    ]};
    const examples = {text: 'Example', number: 1, boolean: true, date: '2026-01-01', url: 'https://example.org', email: 'reader@example.org'};
    const sample = Object.fromEntries(table.schema.map(col => [col.name, col.type === 'select' ? col.options[0] : examples[col.type]]));
    await draft(plan, sample);
  });
  handle('createTable', async () => {
    const table = await mutate('table', {name: $('tableName').value, columns: $('columns').value.split(',').map(value => value.trim())});
    await refresh();
    await showTable(table.id);
  });
  handle('addColumn', async () => {
    const type = $('columnType').value;
    const id = page.table.id;
    const column = {name: $('columnTitle').value, type, unique: $('columnUnique').checked,
      options: type === 'select' ? $('columnChoices').value.split(',').map(value => value.trim()) : []};
    await mutate('table-column', {id, column, expectedRevision: page.table.revision});
    await showTable(id, offset);
  });
  handle('saveDocument', async () => {
    await mutate('document-save', {title: $('docName').value, text: $('docBody').value});
    $('docName').value = '';
    $('docBody').value = '';
    await refresh();
  });
  handle('uploadFile', async () => {
    const file = $('projectFile').files[0];
    if (!file || file.size > 700000) throw Error('Choose a project file up to 700 KB.');
    const payload = {name: file.name};
    if (/\.(png|jpe?g|webp)$/i.test(file.name)) {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = '';
      for (let start = 0; start < bytes.length; start += 8192) binary += String.fromCharCode(...bytes.subarray(start, start + 8192));
      payload.base64 = btoa(binary);
    } else payload.text = await file.text();
    await mutate('file-save', payload);
    $('projectFile').value = '';
    await refresh();
  });
  handle('importTable', async () => {
    const table = await mutate('table-import', {name: $('tableName').value || 'Imported table', csv: $('csv').value});
    await refresh();
    await showTable(table.id);
  });
  $('addRow').onsubmit = event => {
    event.preventDefault();
    attempt(async () => {
      const id = page.table.id;
      await mutate('table-row', {id, values: Object.fromEntries(new FormData(event.target)), expectedRevision: page.table.revision});
      await showTable(id, offset);
    });
  };
  handle('cancelCell', closeCell);
  $('cellEdit').onsubmit = event => {
    event.preventDefault();
    attempt(async () => {
      if (!cell) return;
      const {id, row, column} = cell;
      await mutate('table-cell', {id, row: row.id, column: column.id, expectedVersion: row.version, value: $('cellEditValue').value});
      await showTable(id, offset);
    });
  };
  handle('prevPage', () => showTable(page.table.id, Math.max(0, offset - 25)));
  handle('nextPage', () => showTable(page.table.id, page.nextOffset));
  handle('exportTable', async () => {
    const file = await data('table-export', {id: page.table.id});
    const link = el('a');
    const url = URL.createObjectURL(new Blob([file.csv], {type: 'text/csv;charset=utf-8'}));
    link.href = url;
    link.download = file.name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $('triggerKind').onchange = () => attempt(triggerFields);
  $('target').onchange = () => attempt(showPayload);
  handle('previewSchedule', async () => {
    const result = await request('schedule-preview', {config: config()});
    const list = el('ol');
    list.append(...result.occurrences.map(value => el('li', new Date(value * 1000).toLocaleString())));
    $('triggerResult').replaceChildren(el('h3', 'Upcoming runs'), list);
  });
  handle('armTrigger', async () => {
    const selection = target();
    if (!selection) throw Error('Choose a published workflow.');
    const kind = $('triggerKind').value;
    const result = await request('trigger-create', {...selection, kind: timed() ? 'schedule' : kind,
      config: config(), input: timed() && scheduleInput ? scheduleInput.read() : null});
    const out = $('triggerResult');
    out.replaceChildren(el('p', 'Trigger enabled.'));
    if (result.token) {
      out.append(field('Webhook address', textInput(result.path)), field('Bearer token (shown once)', textInput(result.token)));
      for (const input of out.querySelectorAll('input')) input.readOnly = true;
    }
    await refresh();
  });
  handle('reloadResources', async () => {await refresh(); if (page) await showTable(page.table.id, offset);});
  return {open: async tableId => {
    $('triggerResult').replaceChildren();
    await refresh();
    if (tableId) await showTable(tableId);
  }};
}
