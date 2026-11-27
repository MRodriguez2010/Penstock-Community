import {element, field, labelFor, requestId, selectInput} from './studioFields.js';

function button(text, action, type = 'button') {
  const control = element('button', text, 'studio-quiet');
  control.type = type;
  if (action) control.onclick = action;
  return control;
}

function cadence(timing) {
  if (timing.seconds != null) {
    if (timing.seconds % 3600 === 0) return 'Every ' + timing.seconds / 3600 + ' hr';
    if (timing.seconds % 60 === 0) return 'Every ' + timing.seconds / 60 + ' min';
    return 'Every ' + timing.seconds + ' sec';
  }
  const labels = {minutes: 'Minute', hourly: 'Hourly', daily: 'Daily', weekly: 'Weekly',
    monthly: 'Monthly', custom: 'Custom calendar'};
  return (labels[timing.scheduleType] || 'Calendar') + (timing.timezone ? ' · ' + timing.timezone : '');
}

function actionLabel(action) {
  return {archive: 'Workflow archived', recover: 'Draft recovered', rollback: 'Release activated'}[action] || labelFor(action);
}

export function mountWorkflowLifecycle(root, {request, attempt, project, permission, onChanged}) {
  let selection, details, content, loaded = null, loading = false;

  function clear() {
    selection = null;
    details = null;
    content = null;
    loaded = null;
    loading = false;
    root.replaceChildren();
  }

  async function load(shown) {
    if (loading || loaded || shown !== selection) return;
    loading = true;
    const message = element('p', 'Reading workflow releases…');
    message.setAttribute('role', 'status');
    content.replaceChildren(message);
    try {
      const result = await request('workflow-lifecycle-state', {project: shown.project, workflow: shown.workflow});
      if (shown !== selection) return;
      loaded = result;
      render();
    } catch (error) {
      if (shown === selection) content.replaceChildren(
        element('p', error.message), button('Reload workflow releases', () => load(shown))
      );
    } finally {
      if (shown === selection) loading = false;
    }
  }

  function render(focus = false) {
    const state = loaded;
    if (!state) return;
    const primary = element('div');
    const status = state.archived ? 'Archived' : state.activeVersion != null
      ? 'Active release · version ' + state.activeVersion : 'No active release';
    primary.append(
      element('p', status, 'studio-literal'),
      element('p', 'Latest saved draft is version ' + state.latestVersion + '. Historical versions and receipts stay available.')
    );
    if (permission('admin')) {
      primary.append(state.archived
        ? button('Review draft recovery', () => confirm('recover'))
        : button('Review archive', () => confirm('archive')));
    }
    if (!state.archived && permission('publish')) {
      const choices = state.versions.filter(item => item.published && item.version !== state.activeVersion);
      if (choices.length) {
        const target = selectInput(choices.map(item => [String(item.version), 'Version ' + item.version + ' · ' + item.name]),
          String(choices[0].version));
        primary.append(field('Published version', target),
          button(state.activeVersion == null ? 'Review release activation' : 'Review release change',
            () => confirm('rollback', Number(target.value))));
      }
    }

    content.replaceChildren(primary);
    if (state.history.length) {
      const history = element('details');
      history.append(element('summary', 'Lifecycle history'));
      const list = element('ol');
      for (const receipt of state.history) {
        const description = actionLabel(receipt.action) + (receipt.activeVersion ? ' · version ' + receipt.activeVersion : '');
        const moment = new Date(receipt.at * 1000);
        const item = element('li', description + ' · ');
        const when = element('time', moment.toLocaleString());
        when.dateTime = moment.toISOString();
        item.append(when);
        list.append(item);
      }
      history.append(list);
      content.append(history);
    }
    if (focus) (primary.querySelector('button') || details.querySelector('summary')).focus();
  }

  function confirm(action, version) {
    const state = loaded;
    const selectionAtReview = selection;
    const form = element('form');
    const heading = element('h3', {
      archive: 'Archive this workflow',
      recover: 'Recover this draft',
      rollback: 'Activate published version ' + version,
    }[action]);
    heading.tabIndex = -1;
    const text = {
      archive: 'Archiving pauses its triggers and unstarted deliveries. Saved versions and receipts stay available.',
      recover: 'Recovery returns the workflow to the draft library. Choose a release and enable triggers separately when you are ready.',
      rollback: state.activeVersion == null
        ? 'Version ' + version + ' will become the active release.'
        : 'Active live runs will use version ' + version + ' in place of version ' + state.activeVersion + '.',
    }[action];
    form.append(heading, element('p', text));

    const selected = [];
    if (action === 'rollback') {
      form.append(element('p', 'Schedule pins stay in place unless you select individual schedules below.'));
      const eligible = state.schedules.filter(item => item.version !== version);
      if (eligible.length && permission('admin')) {
        const schedules = element('fieldset');
        schedules.append(element('legend', 'Optionally rebind schedules to version ' + version));
        for (const item of eligible) {
          const check = element('input');
          check.type = 'checkbox';
          selected.push({check, id: item.id, version: item.version, enabled: item.enabled});
          const description = cadence(item.timing) + ' · version ' + item.version
            + (item.enabled ? ' · enabled' : ' · paused') + ' · ' + item.id.slice(-8);
          schedules.append(field(description, check));
        }
        form.append(schedules);
        form.append(element('p', 'Schedules keep their current enabled or paused state. Already queued deliveries keep their original version.'));
      }
    }
    const save = button({archive: 'Archive workflow', recover: 'Recover draft', rollback: 'Activate this release'}[action], null, 'submit');
    const back = button('Back', () => render(true));
    const reload = button('Reload review', () => {
      loaded = null;
      load(selectionAtReview);
    });
    form.append(save, back, reload);
    let intent;
    let id;
    form.onsubmit = event => {
      event.preventDefault();
      if (selection !== selectionAtReview || save.disabled) return;
      const payload = {project: state.project, workflow: state.workflow, expectedGeneration: state.generation};
      if (action === 'rollback') {
        payload.version = version;
        payload.schedules = selected.filter(item => item.check.checked)
          .map(({id: schedule, version: pinned, enabled}) => ({id: schedule, version: pinned, enabled}));
      }
      const next = JSON.stringify(payload);
      if (next !== intent) {
        intent = next;
        id = requestId();
      }
      attempt(async () => {
        save.disabled = back.disabled = reload.disabled = true;
        try {
          const result = await request('workflow-lifecycle-' + action, {...payload, request: id});
          if (selection === selectionAtReview) {
            loaded = null;
            await onChanged?.(result);
            if (selection === selectionAtReview) await load(selectionAtReview);
          }
        } finally {
          save.disabled = back.disabled = reload.disabled = false;
        }
      });
    };
    content.replaceChildren(form);
    heading.focus();
  }

  function show(version) {
    if (!version) {
      clear();
      return;
    }
    const next = {project: project(), workflow: version.workflow, generation: version.generation};
    if (selection && Object.keys(next).every(key => next[key] === selection[key])) return;
    const reopen = next.project === selection?.project && next.workflow === selection?.workflow && details?.open;
    clear();
    selection = next;
    details = element('details', null, 'workflow-lifecycle');
    details.append(element('summary', 'Recovery and releases'));
    content = element('div');
    details.append(content);
    root.append(details);
    details.addEventListener('toggle', () => {
      if (details?.open) load(next);
    });
    if (reopen) details.open = true;
  }

  return {show, clear};
}
