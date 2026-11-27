import {element as el, field, requestId, selectInput, textInput, valueView} from './studioFields.js';

const BRIEF = 'weekly-brief';
const INTAKE = 'reviewed-intake';
const STATUSES = [['complete', 'Completed'], ['in-progress', 'In progress'], ['blocked', 'Blocked']];
const PRIORITY = [['normal', 'Normal'], ['high', 'High'], ['urgent', 'Urgent']];
const COPY = {
  [BRIEF]: {
    name: 'Weekly executive brief',
    description: 'Turn dated updates into a brief for the period you choose. Review the brief before saving a copy to project files.',
    destination: 'An approved brief saves one Markdown copy to Files in this project.',
    steps: [
      ['input', 'Choose updates and dates', 'Take records inside the inclusive date range.'],
      ['prepare', 'Build the executive brief', 'Collect decisions, flag open items overdue at period end, and show each source.'],
      ['review', 'Review the brief', 'Read the prepared brief and choose whether to save it.'],
      ['publish', 'Save the approved copy', 'Write a versioned Markdown file to this project.'],
    ],
  },
  [INTAKE]: {
    name: 'Reviewed intake',
    description: 'Collect a request and decide whether to add it to the project intake table. Every decision has a receipt.',
    destination: 'Approval adds one row to the Reviewed intake table in this project.',
    steps: [
      ['input', 'Capture the request', 'Take a title, requester, priority, and notes.'],
      ['prepare', 'Prepare the table row', 'Show the exact values that would go to the project table.'],
      ['review', 'Review the request', 'Approve or decline this request.'],
      ['publish', 'Add the approved row', 'Save one row to the Reviewed intake project table.'],
    ],
  },
};
const STATUS = {'awaiting-review': 'Awaiting review', 'changes-requested': 'Changes requested',
  approved: 'Approved', declined: 'Declined'};
const EVENT = {waiting: 'Awaiting review', completed: 'Completed', approved: 'Approved', declined: 'Declined',
  'changes-requested': 'Changes requested', skipped: 'Skipped', failed: 'Could not save'};

function localDate() {
  const today = new Date();
  const from = new Date(today);
  from.setDate(today.getDate() - 6);
  const format = day => [day.getFullYear(), String(day.getMonth() + 1).padStart(2, '0'),
    String(day.getDate()).padStart(2, '0')].join('-');
  return {from: format(from), through: format(today)};
}

function calendarDay(value) {
  return new Date(value + 'T00:00:00').toLocaleDateString(undefined, {dateStyle: 'medium'});
}

function stamp(value) {
  return new Date(value).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'});
}

function routePrompt(text) {
  const brief = /\b(weekly|brief|summary|summari[sz]e|executive|report|updates?)\b/i.test(text);
  const intake = /\b(intake|requests?|review|approv\w+|declin\w+|table)\b/i.test(text);
  if (brief === intake) return null;
  return brief ? BRIEF : INTAKE;
}

export function mountStarterStudio(container, {request, project, attempt, openTable, openFile, openJob, onChange,
  fileActionLabel = () => 'Download saved copy'}) {
  let catalog;
  let activeProject;
  let starter;
  let source;
  let inputs;
  let suggestion;
  let pendingRequest;
  let currentRun;
  let inspectedStep;
  let revising;

  const call = (endpoint, body = {}) => request(endpoint, {project: project(), ...body});
  function action(label, fn, cls = '') {
    const node = el('button', label, cls);
    node.type = 'button';
    node.onclick = () => attempt(fn);
    return node;
  }
  function formAction(label, cls) {
    const node = el('button', label, cls);
    node.type = 'submit';
    return node;
  }
  async function load() {
    const id = project();
    if (activeProject !== id) {
      activeProject = id;
      suggestion = null;
      currentRun = null;
      pendingRequest = null;
      revising = null;
      container.replaceChildren();
    }
    catalog = await call('starter-catalog');
  }
  function show(body) {
    container.hidden = false;
    container.replaceChildren(body);
  }
  function heading(title, description, options = {}) {
    const top = el('div', null, 'guided-title');
    const copy = el('div');
    const titleNode = el('h1', title);
    titleNode.tabIndex = -1;
    copy.append(titleNode);
    if (description) copy.append(el('p', description));
    top.append(copy);
    if (options.back) top.append(action('All starters', openHome, 'studio-quiet'));
    return top;
  }
  function focusHeading() {
    container.querySelector('h1')?.focus({preventScroll: true});
    if (container.getBoundingClientRect().top < 0) container.scrollIntoView({block: 'start'});
  }
  function meter(state) {
    const status = el('span', STATUS[state] || state, 'guided-status');
    status.dataset.state = state;
    return status;
  }
  function renderHome() {
    const home = el('div', null, 'guided-home');
    home.append(heading('Start a job',
      'Try either workflow with sample data or use your own. Both run locally without a model or connected account.'));
    const examples = el('div', null, 'guided-starters');
    for (const recipe of catalog.starters) {
      const copy = COPY[recipe.kind];
      const card = el('article', null, 'guided-starter-card');
      card.dataset.kind = recipe.kind;
      card.append(el('h2', recipe.settings.title),
        el('p', copy.description), action('Open ' + copy.name, () => openWorkflow(recipe.kind), 'primary'));
      examples.append(card);
    }
    home.append(examples);

    if (openJob && catalog.permissions.edit) {
      const general = el('form', null, 'guided-other-job');
      const brief = textInput('', {rows: 2, maxLength: 8000, required: true});
      brief.placeholder = 'Example: Draft an executive briefing with decisions, risks, and sources.';
      general.append(el('h2', 'Bring your own job'),
        field('What outcome do you want?', brief),
        el('p', 'Choose a drafting model and review the editable workflow plan before testing.'),
        formAction('Set up job plan', 'studio-quiet'));
      general.onsubmit = event => {
        event.preventDefault();
        const request = brief.value.trim();
        if (request) attempt(() => openJob(request));
      };
      home.append(general);
    }

    const assistant = el('form', null, 'guided-assistant');
    const prompt = textInput(suggestion?.text || '', {required: true, maxLength: 400});
    prompt.placeholder = 'Example: I need a weekly update for leadership';
    assistant.append(el('h2', 'Find a starter'), field('What would you like to do?', prompt),
      formAction('Find a match', 'studio-quiet'));
    const response = el('div', null, 'guided-suggestion');
    response.setAttribute('aria-live', 'polite');
    assistant.append(response);
    function answer() {
      if (!suggestion) return;
      const kind = suggestion.kind;
      const choices = kind ? [kind] : [BRIEF, INTAKE];
      response.replaceChildren(el('p', kind
        ? 'Penstock suggests ' + COPY[kind].name + ' for this task.'
        : 'Do you want a date-based brief or a request that someone decides on?'));
      response.append(...choices.map(choice =>
        action(COPY[choice].name, () => openWorkflow(choice), 'studio-quiet')));
    }
    assistant.onsubmit = event => {
      event.preventDefault();
      const text = prompt.value.trim();
      if (!text) return;
      suggestion = {text, kind: routePrompt(text)};
      answer();
    };
    answer();
    home.append(assistant);

    const waiting = catalog.runs.filter(run => run.status === 'awaiting-review');
    const recent = catalog.runs.filter(run => run.status !== 'awaiting-review');
    for (const [label, records, needsReview] of [
      [catalog.permissions.review ? 'Needs your decision' : 'Awaiting review', waiting, true],
      ['Recent results', recent, false],
    ]) {
      if (!records.length) continue;
      const history = el('section', null, 'guided-history');
      history.append(el('h2', label));
      for (const run of records.slice(0, 6)) {
        const row = action('', () => showRun(run.id), 'guided-history-row');
        const title = el('span', run.title, 'guided-history-title');
        const by = needsReview ? run.submittedBy : run.decidedBy || run.submittedBy;
        const metadata = el('span', [by, 'Version ' + run.version, stamp(run.updatedAt)].filter(Boolean).join(' · '), 'muted');
        row.append(title, metadata, meter(run.status),
          el('span', needsReview && catalog.permissions.review ? 'Review' : 'Open receipt'));
        history.append(row);
      }
      if (needsReview) home.insertBefore(history, examples);
      else home.append(history);
    }
    show(home);
  }

  async function openHome(kind) {
    if (kind === BRIEF || kind === INTAKE) return openWorkflow(kind);
    await load();
    renderHome();
    focusHeading();
  }
  function resetInput(nextSource) {
    if (nextSource === 'sample') return structuredClone(starter.sample);
    if (starter.kind === BRIEF) {
      const range = localDate();
      return {...range, records: [newUpdate(range.through)]};
    }
    return {title: '', requester: '', notes: '', priority: starter.settings.defaultPriority};
  }
  function newUpdate(date) {
    return {date, title: '', owner: '', status: 'complete', due: '', decision: '', source: ''};
  }
  async function openWorkflow(kind, previous = null) {
    await load();
    starter = catalog.starters.find(value => value.kind === kind);
    if (!starter) throw Error('This starter is unavailable.');
    source = previous?.source || 'sample';
    inputs = previous ? structuredClone(previous.input) : resetInput(source);
    revising = previous;
    inspectedStep = 'input';
    currentRun = null;
    renderInputs();
    focusHeading();
  }
  function statusByStep(run) {
    return new Map(run?.history.map(entry => [entry.step, entry.state]) || []);
  }
  function workflowAside(kind, run) {
    const aside = el('aside', null, 'guided-context');
    aside.append(el('h2', 'Workflow steps'));
    const steps = el('ol', null, 'guided-step-list');
    const detail = el('p', null, 'guided-step-detail');
    const buttons = [];
    function select(step) {
      inspectedStep = step;
      for (const [id, node] of buttons) node.setAttribute('aria-pressed', String(id === step));
      detail.textContent = COPY[kind].steps.find(([id]) => step === id)?.[2] || '';
    }
    const statuses = statusByStep(run);
    for (const [id, label] of COPY[kind].steps) {
      const item = el('li');
      const button = el('button', label);
      button.type = 'button';
      button.onclick = () => select(id);
      if (statuses.has(id)) button.append(el('small', EVENT[statuses.get(id)]));
      buttons.push([id, button]);
      item.append(button);
      steps.append(item);
    }
    select(inspectedStep);
    aside.append(steps, detail);
    if (!run && suggestion?.kind === kind) {
      const chat = el('div', null, 'guided-context-message');
      chat.append(el('h3', 'Your request'), el('p', suggestion.text), el('p', 'Penstock opened this starter from your request.'));
      aside.append(chat);
    }
    return aside;
  }
  function buildWorkspace(version, body, run) {
    const workspace = el('div', null, 'guided-page');
    const copy = COPY[version.kind];
    const top = heading(version.settings.title, run ? null : copy.description, {back: true});
    const meta = el('div', null, 'guided-meta');
    meta.append(el('span', 'Version ' + version.version), el('span', 'Local workflow'));
    if (run) {
      workspace.dataset.runId = run.id;
      meta.append(meter(run.status));
    }
    else if (catalog.permissions.edit) meta.append(action('Workflow settings', renderSettings, 'studio-quiet'));
    const layout = el('div', null, 'guided-workspace');
    layout.append(body, workflowAside(version.kind, run));
    workspace.append(top, meta, layout);
    show(workspace);
  }
  function projectTablePicker(form) {
    const picker = el('section', null, 'guided-table-source');
    picker.append(action('Use a project table', async () => {
      const result = await call('starter-table-list');
      if (!form.isConnected) return;
      picker.replaceChildren(el('h3', 'Use a project table'));
      if (!result.tables.length) {
        picker.append(el('p', 'Add a project table with Date, Title and Status columns, or enter updates below.'));
        return;
      }
      const select = selectInput(result.tables.map(t => [t.id, t.name]), result.tables[0].id);
      const status = el('p', 'Loading a table replaces the updates below with an editable local copy.');
      picker.append(field('Updates table', select), status, action('Load updates', async () => {
        const selected = select.value;
        const snapshot = await call('starter-table-input', {table: selected});
        if (!form.isConnected || select.value !== selected) return;
        inputs = snapshot.input;
        renderInputs();
        const message = el('p', 'Loaded ' + inputs.records.length + ' updates from ' + snapshot.name + '. Review them before continuing.');
        message.setAttribute('role', 'status');
        container.querySelector('.guided-table-source').prepend(message);
      }, 'studio-quiet'));
      select.focus();
    }, 'studio-quiet'));
    form.append(picker);
  }
  function briefFields(form) {
    const dates = el('div', null, 'guided-fields two');
    for (const [key, label] of [['from', 'Period start'], ['through', 'Period end']]) {
      const date = textInput(inputs[key], {type: 'date', required: true});
      date.name = key;
      date.oninput = () => {inputs[key] = date.value;};
      dates.append(field(label, date));
    }
    form.append(dates);
    if (source === 'sample') {
      const updates = el('details');
      updates.append(el('summary', 'View ' + inputs.records.length + ' sample updates'));
      updates.append(updateTable(inputs.records, true));
      form.append(updates);
      return;
    }
    if (catalog.permissions.run) projectTablePicker(form);
    const updates = el('fieldset', null, 'guided-updates');
    updates.append(el('legend', 'Updates'));
    const rows = el('div', null, 'guided-update-rows');
    const add = action('Add an update', () => {
      inputs.records.push(newUpdate(inputs.through));
      renderRows();
      rows.lastElementChild?.querySelector('input')?.focus();
    }, 'studio-quiet');
    function renderRows() {
      rows.replaceChildren();
      inputs.records.forEach((record, index) => {
        const row = el('div', null, 'guided-update');
        for (const [key, label, type, required, maxLength] of [
          ['date', 'Date', 'date', true], ['title', 'Update', 'text', true, 240],
          ['owner', 'Owner', 'text', false, 100],
          ['due', 'Due date', 'date', false], ['decision', 'Decision made', 'text', false, 240],
          ['source', 'Source or reference', 'text', false, 180],
        ]) {
          const input = textInput(record[key], {type, required, maxLength});
          input.oninput = () => {record[key] = input.value;};
          row.append(field(label, input));
        }
        const status = selectInput(STATUSES, record.status);
        status.onchange = () => {record.status = status.value;};
        row.append(field('Status', status));
        if (inputs.records.length > 1) {
          const remove = action('Remove update', () => {
            inputs.records.splice(index, 1);
            renderRows();
            add.focus();
          }, 'studio-quiet');
          remove.setAttribute('aria-label', 'Remove update ' + (index + 1));
          row.append(remove);
        }
        rows.append(row);
      });
      add.disabled = inputs.records.length >= 40;
    }
    renderRows();
    updates.append(rows, add);
    form.append(updates);
  }
  function intakeFields(form) {
    const group = el('div', null, 'guided-fields');
    const priority = selectInput(PRIORITY, inputs.priority);
    priority.disabled = source === 'sample';
    priority.onchange = () => {inputs.priority = priority.value;};
    for (const [key, label, options] of [
      ['title', 'Request title', {required: true, maxLength: 180}],
      ['requester', 'Requested by', {required: true, maxLength: 120}],
      ['notes', 'Notes', {rows: 3, maxLength: 2000}],
    ]) {
      const input = textInput(inputs[key], options);
      input.readOnly = source === 'sample';
      input.oninput = () => {inputs[key] = input.value;};
      group.append(field(label, input));
      if (key === 'requester') group.append(field('Priority', priority));
    }
    form.append(group);
  }
  function renderInputs() {
    const form = el('form', null, 'guided-main');
    form.append(el('h2', 'Set up a run'));
    if (revising) {
      const requested = el('section', null, 'guided-change-request');
      requested.append(el('h3', 'Requested change'), el('p', revising.changeRequest.reason));
      form.append(requested);
    }
    const select = selectInput([['sample', 'Sample data'], ['custom', 'Your data']], source);
    select.onchange = () => {
      source = select.value;
      inputs = resetInput(source);
      renderInputs();
      container.querySelector('#starterDataSource')?.focus();
    };
    select.id = 'starterDataSource';
    form.append(field('Data source', select), el('p', source === 'sample'
      ? 'Sample records are fictional. Running this creates a saved, local review receipt.'
      : 'This run processes the information you enter in this project. Its inputs stay with the run receipt.', 'guided-explainer'));
    if (starter.kind === BRIEF) briefFields(form);
    else intakeFields(form);
    const actions = el('div', null, 'guided-primary-actions');
    const run = formAction(starter.kind === BRIEF ? 'Generate brief for review' : 'Submit for review', 'primary');
    run.disabled = !catalog.permissions.run;
    actions.append(run, el('p', COPY[starter.kind].destination));
    form.append(actions);
    if (run.disabled) form.append(el('p', 'Your project access allows viewing existing runs.', 'guided-explainer'));
    form.onsubmit = event => {
      event.preventDefault();
      attempt(async () => {
        const draft = {kind: starter.kind, version: starter.version, source, input: inputs,
          ...(revising ? {revises: revising.id} : {})};
        const intent = JSON.stringify(draft);
        if (pendingRequest?.intent !== intent) pendingRequest = {intent, id: requestId()};
        const result = await call('starter-run', {...draft, request: pendingRequest.id});
        pendingRequest = null;
        currentRun = result;
        inspectedStep = 'review';
        revising = null;
        renderReceipt();
        onChange?.(result);
        focusHeading();
      });
    };
    buildWorkspace(starter, form);
  }

  function renderSettings() {
    if (!catalog.permissions.edit) return;
    const form = el('form', null, 'guided-main');
    const title = textInput(starter.settings.title, {required: true, maxLength: 100});
    const question = textInput(starter.settings.reviewQuestion, {required: true, maxLength: 300});
    form.append(el('h2', 'Workflow settings'), field('Workflow name', title), field('Review question', question));
    let configuration;
    if (starter.kind === BRIEF) {
      const owners = selectInput([['yes', 'Show owners'], ['no', 'Hide owners']], starter.settings.includeOwners ? 'yes' : 'no');
      form.append(field('Owners in the brief', owners));
      configuration = () => ({includeOwners: owners.value === 'yes'});
    } else {
      const priority = selectInput(PRIORITY, starter.settings.defaultPriority);
      form.append(field('Default priority for your data', priority));
      configuration = () => ({defaultPriority: priority.value});
    }
    form.append(el('p', 'Saving changes creates a new version. Existing receipts keep the version they ran.', 'guided-explainer'));
    const actions = el('div', null, 'actions');
    actions.append(formAction('Save version', 'primary'), action('Back to run', renderInputs, 'studio-quiet'));
    form.append(actions);
    form.onsubmit = event => {
      event.preventDefault();
      attempt(async () => {
        const configured = await call('starter-save', {
          kind: starter.kind, baseVersion: starter.version,
          settings: {title: title.value, reviewQuestion: question.value, ...configuration()},
        });
        starter = {...starter, ...configured};
        renderInputs();
      });
    };
    buildWorkspace(starter, form);
  }

  function updateTable(updates, includeOwners) {
    const frame = el('div', null, 'records guided-update-table');
    const table = el('table');
    const heading = el('thead');
    const keys = el('tr');
    const columns = ['Date', 'Update', ...(includeOwners ? ['Owner'] : []), 'Status', 'Source'];
    for (const name of columns) {
      const th = el('th', name);
      th.scope = 'col';
      keys.append(th);
    }
    heading.append(keys);
    const body = el('tbody');
    for (const record of updates) {
      const row = el('tr');
      const values = [calendarDay(record.date), record.title, ...(includeOwners ? [record.owner || 'Unassigned'] : []),
        STATUSES.find(([name]) => name === record.status)?.[1] || '', record.source];
      row.append(...values.map(text => el('td', text)));
      body.append(row);
    }
    table.append(heading, body);
    frame.append(table);
    return frame;
  }
  function briefHighlights(label, items, empty, itemDate) {
    const section = el('section', null, 'guided-highlights');
    section.append(el('h3', label));
    if (!items.length) {
      section.append(el('p', empty));
      return section;
    }
    const list = el('ul');
    for (const item of items) {
      const line = el('li');
      line.append(el('strong', item.title),
        el('small', itemDate(item) + ' · Source: ' + item.source));
      list.append(line);
    }
    section.append(list);
    return section;
  }
  function resultParts(run) {
    const section = el('section', null, 'guided-result');
    if (run.kind === INTAKE) {
      const {Decision, ...row} = run.output.row;
      section.append(el('h2', 'Request details'), valueView(row));
      return [section, null];
    }
    const {period, counts, updates, total, decisions = [], overdue = []} = run.output;
    section.append(el('h2', 'Executive brief'),
      el('p', calendarDay(period.from) + ' to ' + calendarDay(period.through), 'guided-period'),
      el('p', total + (total === 1 ? ' update in this period' : ' updates in this period')));
    const metrics = el('dl', null, 'guided-metrics');
    for (const [key, label] of STATUSES) {
      const item = el('div');
      item.append(el('dt', label), el('dd', counts[key]));
      metrics.append(item);
    }
    section.append(metrics, el('p', [
      decisions.length + (decisions.length === 1 ? ' decision' : ' decisions'),
      overdue.length + (overdue.length === 1 ? ' overdue item' : ' overdue items'),
    ].join(' · '), 'guided-brief-summary'));
    const details = el('section', null, 'guided-result-detail');
    details.append(
      briefHighlights('Decisions', decisions, 'No decisions were recorded.', item => calendarDay(item.date)),
      briefHighlights('Overdue items', overdue, 'No open items were due before the period end.', item => 'Due ' + calendarDay(item.due)));
    if (updates.length) details.append(updateTable(updates, run.settings.includeOwners));
    else details.append(el('p', 'No updates fall within these dates. Start another run with a different period to include other updates.'));
    return [section, details];
  }
  function showChangeForm(delivery, run) {
    if (delivery.querySelector('.guided-change-form')) return;
    const form = el('form', null, 'guided-change-form');
    const reason = textInput('', {rows: 3, maxLength: 1000, required: true});
    const actions = el('div', null, 'actions');
    actions.append(formAction('Send change request', 'primary'),
      action('Cancel', () => {form.remove(); delivery.querySelector('.guided-request-change')?.focus();}, 'studio-quiet'));
    form.append(field('What should change?', reason), actions);
    form.onsubmit = event => {
      event.preventDefault();
      attempt(async () => {
        currentRun = await call('starter-request-changes', {id: run.id, reason: reason.value});
        renderReceipt();
        onChange?.(currentRun);
        focusHeading();
      });
    };
    delivery.append(form);
    reason.focus();
  }
  function renderReceipt() {
    const run = currentRun;
    const body = el('div', null, 'guided-main guided-receipt');
    const receiptTitle = el('div', null, 'guided-receipt-label');
    receiptTitle.append(el('h2', 'Run receipt'), el('p', stamp(run.createdAt) + ' · ' +
      (run.source === 'sample' ? 'Sample data' : 'Your data')));
    const [summary, details] = resultParts(run);
    body.append(receiptTitle, summary);
    const delivery = el('div', null, 'guided-review');
    if (run.status === 'awaiting-review') {
      delivery.append(el('h3', run.settings.reviewQuestion), el('p', COPY[run.kind].destination));
      const recent = run.history.at(-1);
      if (recent?.state === 'failed') {
        const failure = el('p', recent.message, 'guided-error');
        failure.setAttribute('role', 'alert');
        delivery.append(failure);
      }
      if (catalog.permissions.review) {
        const decisions = el('div', null, 'actions');
        for (const [label, approve, style] of [['Approve', true, 'primary'], ['Decline', false, 'studio-quiet']]) {
          decisions.append(action(label, async () => {
            currentRun = await call('starter-decide', {id: run.id, approve});
            inspectedStep = currentRun.status === 'awaiting-review' ? 'review' : 'publish';
            renderReceipt();
            onChange?.(currentRun);
          }, style));
        }
        decisions.append(action('Request changes', () => showChangeForm(delivery, run), 'studio-quiet guided-request-change'));
        delivery.append(decisions);
      } else delivery.append(el('p', 'A project reviewer can approve or decline this run.'));
    } else if (run.status === 'changes-requested') {
      const by = run.changeRequest.actor?.displayName;
      delivery.append(el('h3', 'Changes requested'), el('p', run.changeRequest.reason, 'studio-literal'));
      if (by) delivery.append(el('small', 'Requested by ' + by));
      const actions = el('div', null, 'actions');
      if (run.revisedBy) actions.append(action('Open revised preview', () => showRun(run.revisedBy), 'primary'));
      else if (catalog.permissions.run) actions.append(action('Revise and preview', () => openWorkflow(run.kind, run), 'primary'));
      else actions.append(el('p', 'A project editor can submit a revised preview.'));
      delivery.append(actions);
    } else if (run.effect) {
      const {type, name, version} = run.effect;
      const change = type === 'file' ? 'Saved ' + name + ' · version ' + version : 'Added a row to ' + name;
      delivery.append(el('h3', change));
      if (type === 'file' && openFile) delivery.append(action(fileActionLabel(), () => openFile(run.effect, run), 'studio-quiet'));
      if (type === 'table' && openTable) delivery.append(action('Open project table', () => openTable(run.effect, run), 'studio-quiet'));
    } else {
      delivery.append(el('h3', 'Declined'), el('p', 'This run made no change to project files or tables.'));
    }
    body.append(delivery);
    if (details) body.append(details);
    const timeline = el('details', null, 'guided-timeline');
    timeline.append(el('summary', 'Run activity'));
    const history = el('ol');
    for (const event of run.history) {
      const label = COPY[run.kind].steps.find(([id]) => id === event.step)?.[1] || event.step;
      const actor = event.actor?.displayName;
      const item = el('li', label + ' · ' + EVENT[event.state] + (actor ? ' · ' + actor : ''));
      item.append(el('small', stamp(event.at)));
      history.append(item);
    }
    timeline.append(history);
    const links = el('div', null, 'actions');
    links.append(action('Refresh receipt', () => showRun(run.id), 'studio-quiet'));
    if (run.revises) links.append(action('Open previous preview', () => showRun(run.revises), 'studio-quiet'));
    if (catalog.permissions.run) links.append(action('Start another run', () => openWorkflow(run.kind), 'studio-quiet'));
    body.append(timeline, links);
    buildWorkspace(run, body, run);
  }
  async function showRun(id) {
    await load();
    currentRun = await call('starter-read', {id});
    inspectedStep = currentRun.status === 'awaiting-review' ? 'review' : 'publish';
    renderReceipt();
    focusHeading();
  }
  return {open: openHome, openRun: showRun};
}
