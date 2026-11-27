import {element as el, labelFor, valueView} from './studioFields.js';

const CLOSED = new Set(['finished', 'failed', 'declined', 'interrupted']);
const STATUS = {
  matched: 'Matches the receipt',
  different: 'Differs from the receipt',
  blocked: 'Replay stopped',
};
const STEPS = {
  matched: 'Matches',
  changed: 'Changed',
  added: 'New step on this path',
  'not-reached': 'Not reached in replay',
  blocked: 'Cannot replay',
};
const FIELDS = {port: 'Route', output: 'Output', error: 'Error', decision: 'Decision', inputs: 'Prepared inputs'};

function evidence(title, fields) {
  const panel = el('section');
  panel.append(el('h4', title));
  if (!fields) {
    panel.append(el('p', 'No recorded value.'));
    return panel;
  }
  for (const [field, item] of Object.entries(fields)) {
    panel.append(el('p', FIELDS[field] || labelFor(field)));
    if (Object.hasOwn(item, 'value')) {
      panel.append(valueView(item.value));
    } else {
      panel.append(el('p', item.bytes.toLocaleString() + ' bytes'), el('code', 'SHA-256 ' + item.sha256));
    }
  }
  return panel;
}

function comparison(result) {
  const section = el('section');
  section.append(el('h3', STATUS[result.status]));
  section.append(el('p', 'Workflow version ' + result.version + ' · Saved state ' + labelFor(result.expectedState) +
    (result.observedState ? ' · Replay state ' + labelFor(result.observedState) : '')));
  if (result.reason) section.append(el('p', result.reason));
  if (result.orderChanged) section.append(el('p', 'The replay visited the same steps in a different order.'));
  if (result.outcome) {
    const outcome = el('details');
    outcome.append(el('summary', result.outcome.changed ? 'Final result changed' : 'Final result matches'),
      evidence('Saved receipt', result.outcome.expected), evidence('Replay', result.outcome.observed));
    section.append(outcome);
  }
  for (const step of result.steps) {
    const details = el('details');
    const title = (step.label || labelFor(step.kind)) + ' · ' + STEPS[step.status];
    details.append(el('summary', title));
    if (step.reason) details.append(el('p', step.reason));
    if (step.basis) details.append(el('p', step.basis));
    if (step.changes.length) {
      details.append(el('p', 'Changed fields: ' + step.changes.map(field => FIELDS[field]).join(', ')));
    }
    details.append(evidence('Saved receipt', step.expected), evidence('Replay', step.observed));
    section.append(details);
  }
  return section;
}

export function mountReceiptReplay(host, request) {
  const section = el('section');
  const trigger = el('button', 'Replay receipt offline');
  trigger.type = 'button';
  const status = el('p');
  status.setAttribute('role', 'status');
  const report = el('div');
  section.append(trigger, status, report);
  section.hidden = true;
  host.after(section);

  let selection;
  let scope;
  trigger.onclick = async () => {
    if (trigger.disabled || !selection) return;
    const pending = selection;
    trigger.disabled = true;
    status.textContent = 'Comparing the saved inputs and workflow version…';
    report.replaceChildren();
    try {
      const result = await request('receipt-replay', pending);
      if (pending !== selection) return;
      status.textContent = 'Offline comparison ready.';
      report.replaceChildren(comparison(result));
    } catch (error) {
      if (pending === selection) status.textContent = error.message;
    } finally {
      if (pending === selection) trigger.disabled = false;
    }
  };

  return {
    show(run, version, project, allowed) {
      const available = allowed && run && version && version.digest && CLOSED.has(run.state);
      const key = available ? JSON.stringify([project, run.id, version.workflow, version.version, version.digest]) : null;
      section.hidden = !available;
      if (key === scope) return;
      scope = key;
      selection = available ? {project, run: run.id, workflow: version.workflow,
        version: version.version, digest: version.digest} : null;
      trigger.disabled = false;
      status.textContent = '';
      report.replaceChildren();
    },
  };
}
