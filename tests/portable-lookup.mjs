import assert from 'node:assert/strict';
import test from 'node:test';
import {planEditor} from '../src/studioFields.js';

class Element {
  constructor(tag) {Object.assign(this, {tag, children: [], attributes: {}, value: '', textContent: ''});}
  append(...items) {this.children.push(...items);}
  prepend(...items) {this.children.unshift(...items);}
  replaceChildren(...items) {this.children = items;}
  setAttribute(name, value) {this.attributes[name] = value;}
  add(option) {
    this.append(option);
    if (option.selected || this.children.length === 1) this.value = option.value;
  }
  querySelector(tag) {return find(this, item => item.tag === tag);}
  checkValidity() {return Number.isInteger(Number(this.value)) && Number(this.value) >= +this.min && Number(this.value) <= +this.max;}
  focus() {}
}

globalThis.document = {createElement: tag => new Element(tag)};
globalThis.Option = class {
  constructor(label, value, _default, selected) {Object.assign(this, {label, value, selected});}
};

function find(node, accepts) {
  return accepts(node) ? node : (node.children || []).map(child => find(child, accepts)).find(Boolean);
}
function field(root, label) {return find(root, node => node.tag === 'label' && node.textContent === label).children[0];}
function click(root, text) {find(root, node => node.tag === 'button' && node.textContent === text).onclick();}

const plan = {name: 'Lookup plan', steps: [{id: 'start', type: 'start', next: 'done'}, {id: 'done', type: 'finish'}]};
const adapters = {
  search: {kind: 'web-search', host: 'search.example.org'},
  repositories: {kind: 'github', host: 'repositories.example.org'},
  directory: {kind: 'enrichment', host: 'directory.example.org'},
};

test('normal step insertion exposes only the enabled lookup types for this project', () => {
  const editor = planEditor(plan, action => action(), {lookupAdapters: {directory: adapters.directory}});
  const insert = field(editor.node, 'Add step');
  const options = insert.children.map(option => option.value);
  assert.ok(options.includes('enrichment'));
  assert.ok(!options.includes('github') && !options.includes('web-search'));
  insert.value = 'enrichment';
  click(editor.node, 'Insert step');
  const operation = field(editor.node, 'Lookup operation');
  assert.deepEqual(operation.children.map(option => option.value), ['person', 'company']);
  assert.ok(find(editor.node, node => node.textContent?.includes('directory.example.org')));
  assert.equal(editor.read().steps[1].config.adapter, 'directory');
  assert.equal(plan.steps.length, 2);
});

test('repository operation, previous output and limit persist in the recovered guided draft', () => {
  const editor = planEditor(plan, action => action(), {lookupAdapters: adapters});
  field(editor.node, 'Add step').value = 'github';
  click(editor.node, 'Insert step');
  const operation = field(editor.node, 'Lookup operation');
  operation.value = 'pull-requests';
  operation.onchange();
  const mode = field(editor.node, 'Input source');
  mode.value = '$previous';
  mode.onchange();
  field(editor.node, 'Maximum results').value = '8';
  const saved = editor.snapshot();
  const recovered = planEditor(saved.plan, action => action(), {recovered: saved, lookupAdapters: adapters});
  assert.deepEqual(recovered.read().steps[1].config, {
    adapter: 'repositories', operation: 'pull-requests', query: {ref: {node: '$previous'}}, limit: 8,
  });
});

test('configured infrastructure and weather blocks expose a typed ID and a single result', () => {
  const names = [
    ['service-health', 'Service ID', 'example-service'],
    ['worker-status', 'Worker ID', 'example-worker'],
    ['published-workflow', 'Published workflow ID', 'example-workflow'],
    ['weather-observation', 'Weather station ID', 'example-station'],
  ];
  for (const [kind, label, query] of names) {
    const lookupAdapters = {operations: {kind, host: 'operations.example.org'}};
    const editor = planEditor(plan, action => action(), {lookupAdapters});
    const insert = field(editor.node, 'Add step');
    assert.equal(insert.children.filter(option => names.some(([name]) => name === option.value)).length, 1);
    insert.value = kind;
    click(editor.node, 'Insert step');
    const operation = field(editor.node, 'Lookup operation');
    assert.deepEqual(operation.children.map(option => option.value), [kind]);
    const input = find(editor.node, node => node.tag === 'label' && node.children[0]?.textContent === label).children[1];
    assert.equal(input.value, query);
    assert.equal(field(editor.node, 'Maximum results').max, '1');
    assert.deepEqual(editor.read().steps[1].config, {adapter: 'operations', operation: kind, query, limit: 1});
  }
});
