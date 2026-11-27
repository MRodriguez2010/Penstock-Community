import assert from 'node:assert/strict';
import test from 'node:test';
import {planEditor, resultView, valueView} from '../src/studioFields.js';
import {mountStarterStudio} from '../src/starterStudio.js';

class Element {
  constructor(tag) {
    this.localName = tag;
    this.childNodes = [];
    this.dataset = {};
    this.attributes = {};
    this.textContent = '';
    this.value = '';
  }
  append(...items) { this.childNodes.push(...items); }
  replaceChildren(...items) { this.childNodes = items; }
  setAttribute(name, value) { this.attributes[name] = value; }
  add(option) {
    this.append(option);
    if (option.selected || this.childNodes.length === 1) this.value = option.value;
  }
  querySelector(tag) { return find(this, node => node.localName === tag); }
  focus() {}
  getBoundingClientRect() { return {top: 0}; }
}
globalThis.document = {createElement: tag => new Element(tag)};
globalThis.Option = class {
  constructor(label, value, _default, selected) { Object.assign(this, {label, value, selected}); }
};

function find(node, predicate) {
  if (predicate(node)) return node;
  return (node.childNodes || []).map(child => find(child, predicate)).find(Boolean);
}
function text(node) {
  if (typeof node === 'string') return node;
  return node.textContent + (node.childNodes || []).map(text).join(' ');
}
function button(node, label) {
  return find(node, item => item.localName === 'button' && item.textContent === label);
}

test('format executive results as headings and lists and hide protected values', () => {
  const report = resultView('## Decisions\n\n- Record the outcome.\n\n## Next steps\n\n1. Review the source.');
  assert.equal(find(report, node => node.localName === 'h4').textContent, 'Decisions');
  assert.equal(find(report, node => node.localName === 'ol').childNodes[0].textContent, 'Review the source.');
  const protectedFields = valueView({token: 'example-token', failure: {code: 403, detail: 'Bearer example-token'},
    sourceCode: 'return process.env.EXAMPLE;'});
  assert.match(text(protectedFields), /403/);
  assert.match(text(protectedFields), /Code hidden/);
  assert.match(text(protectedFields), /Bearer \[hidden\]/);
  assert.doesNotMatch(text(protectedFields), /example-token|process\.env/);
  const encoded = '```json\n{"password":"dummy-password","count":2}\n```';
  assert.doesNotMatch(text(resultView(encoded)), /dummy-password/);
  assert.match(text(resultView(encoded)), /2/);
});

test('insert offline grouping and ordered conditions with stable route IDs', async () => {
  const source = {name: 'Local workflow', steps: [
    {id: 'start', type: 'start', next: 'finish'},
    {id: 'finish', type: 'finish'},
  ]};
  const editor = planEditor(source, action => action());
  function kinds() {
    return find(editor.node, node => node.localName === 'select' &&
      node.childNodes.some(option => option.value === 'ordered-condition'));
  }
  kinds().value = 'group-records';
  await button(editor.node, 'Insert step').onclick();
  kinds().value = 'ordered-condition';
  await button(editor.node, 'Insert step').onclick();
  const branchLabel = find(editor.node, node => node.localName === 'label' && node.textContent === 'Branch name');
  branchLabel.childNodes[0].value = 'Urgent route';
  const plan = editor.read();
  const [start, group, condition, end] = plan.steps;
  assert.equal(start.next.success, group.id);
  assert.deepEqual(group.config, {field: 'status'});
  assert.deepEqual(group.next, {success: condition.id, error: end.id});
  assert.deepEqual(condition.next, {match: end.id, default: end.id, error: end.id});
  assert.equal(condition.config.branches[0].id, 'match');
  assert.equal(condition.config.branches[0].label, 'Urgent route');
  assert.equal(source.steps.length, 2);
});

test('make both offline recipes and the separate arbitrary job path discoverable', async () => {
  const host = new Element('section');
  const requests = [];
  const jobs = [];
  const mount = mountStarterStudio(host, {
    project: () => 'project',
    request: (name, body) => {
      requests.push({name, ...body});
      return {runs: [], permissions: {edit: true}, starters: [
        {kind: 'weekly-brief', settings: {title: 'Weekly executive brief'}},
        {kind: 'reviewed-intake', settings: {title: 'Reviewed intake'}},
      ]};
    },
    attempt: action => action(),
    openJob: brief => jobs.push(brief),
  });
  await mount.open();
  assert.ok(button(host, 'Open Weekly executive brief'));
  assert.ok(button(host, 'Open Reviewed intake'));
  const job = find(host, node => node.className === 'guided-other-job');
  find(job, node => node.localName === 'textarea').value = 'Review new rollout decisions.';
  job.onsubmit({preventDefault() {}});
  assert.deepEqual(jobs, ['Review new rollout decisions.']);
  assert.deepEqual(requests, [{name: 'starter-catalog', project: 'project'}]);
});
