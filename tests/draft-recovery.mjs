import assert from 'node:assert/strict';
import test from 'node:test';
import {createDraftRecovery, draftRecoveryStatus, planEditor} from '../src/studioFields.js';

class MemoryStorage {
  #records = new Map();
  get length() {return this.#records.size;}
  key(index) {return [...this.#records.keys()][index] ?? null;}
  getItem(key) {return this.#records.get(key) ?? null;}
  setItem(key, value) {this.#records.set(key, value);}
  removeItem(key) {this.#records.delete(key);}
}

class Element {
  constructor(tag) {Object.assign(this, {tag, children: [], attributes: {}, value: '', textContent: ''});}
  append(...items) {this.children.push(...items);}
  replaceChildren(...items) {this.children = items;}
  setAttribute(name, value) {this.attributes[name] = value;}
  add(option) {
    this.append(option);
    if (option.selected || this.children.length === 1) this.value = option.value;
  }
  querySelector(tag) {return find(this, item => item.tag === tag);}
  focus() {}
}

globalThis.document = {createElement: tag => new Element(tag)};
globalThis.Option = class {
  constructor(label, value, _default, selected) {Object.assign(this, {label, value, selected});}
};

function find(element, accepts) {
  if (accepts(element)) return element;
  return (element.children || []).map(child => find(child, accepts)).find(Boolean);
}
function labeled(node, label) {
  return find(node, item => item.tag === 'label' && item.textContent === label).children[0];
}

test('isolate local drafts by signed-in user, project, role, and workflow version', () => {
  const browser = new MemoryStorage();
  const first = createDraftRecovery(browser, 'user-one', 'project-one', 'editor');
  const otherAccount = createDraftRecovery(browser, 'user-two', 'project-one', 'editor');
  const otherProject = createDraftRecovery(browser, 'user-one', 'project-two', 'editor');
  const otherRole = createDraftRecovery(browser, 'user-one', 'project-one', 'operator');
  const baseline = {id: 'copy', at: 4, workflow: 'workflow-one', baseVersion: 2, brief: 'Saved only by explicit write'};
  assert.deepEqual(first.list(), []);
  first.write(baseline);
  assert.deepEqual(otherAccount.list(), []);
  assert.deepEqual(otherRole.list(), []);
  assert.deepEqual(otherProject.list(), []);
  otherAccount.write({...baseline, brief: 'Different account'});
  otherRole.write({...baseline, brief: 'Different role'});
  otherProject.write({...baseline, brief: 'Different project'});
  const latest = [{workflow: 'workflow-one', version: 2}];
  assert.equal(draftRecoveryStatus(first.list()[0], latest), 'current');
  assert.equal(draftRecoveryStatus(first.list()[0], [...latest, {workflow: 'workflow-one', version: 3}]), 'newer');
  assert.equal(draftRecoveryStatus(first.list()[0], [{...latest[0], archived: true}]), 'unavailable');
  first.discard(baseline.id);
  assert.deepEqual(first.list(), []);
  assert.equal(otherRole.list().length, 1);
  first.clearAccount();
  assert.deepEqual(otherRole.list(), []);
  assert.deepEqual(otherProject.list(), []);
  assert.equal(otherAccount.list()[0].brief, 'Different account');
});

test('recover the selected step and in-progress fields without changing the saved workflow', () => {
  const attempt = action => action();
  const stored = {name: 'Saved version', steps: [
    {id: 'start', type: 'start', next: 'compose'},
    {id: 'compose', type: 'response', label: 'Compose result', config: {value: {status: 'pending'}}, next: 'finish'},
    {id: 'finish', type: 'finish'},
  ]};
  const edit = planEditor(stored, attempt, {step: 'compose'});
  labeled(edit.node, 'Workflow name').value = 'My working copy';
  labeled(edit.node, 'Step name').value = 'Summarize the draft';
  find(edit.node, item => item.tag === 'button' && item.textContent === 'Add field').onclick();
  const unnamed = find(edit.node, item => item.attributes?.['aria-label'] === 'Field name' && item.value === '');
  assert.ok(unnamed);
  const snapshot = JSON.parse(JSON.stringify(edit.snapshot()));
  const reopened = planEditor(snapshot.plan, attempt, {recovered: snapshot});
  assert.equal(labeled(reopened.node, 'Workflow name').value, 'My working copy');
  assert.equal(labeled(reopened.node, 'Step name').value, 'Summarize the draft');
  assert.equal(find(reopened.node, item => item.attributes?.['aria-pressed'] === 'true').textContent, 'Compose result');
  assert.throws(() => reopened.read(), /distinct name/);
  find(reopened.node, item => item.attributes?.['aria-label'] === 'Field name' && item.value === '').value = 'source';
  assert.deepEqual(reopened.read().steps[1].config.value, {status: 'pending', source: ''});
  assert.equal(stored.name, 'Saved version');
  assert.equal(stored.steps[1].label, 'Compose result');
});
