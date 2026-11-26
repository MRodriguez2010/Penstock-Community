import assert from 'node:assert/strict';
import test from 'node:test';
import {executionOverlay} from '../src/workflowEvidence.js';

const graph = {
  nodes: [
    {id:'start', type:'start', position:{x:0,y:0}, config:{}},
    {id:'check', type:'condition', position:{x:200,y:0}, configRevision:1,
      config:{rules:[{operator:'above',value:3}]}},
    {id:'done', type:'finish', position:{x:400,y:0}, config:{}},
    {id:'review', type:'finish', position:{x:400,y:200}, config:{}},
  ],
  edges: [
    {id:'start-check',source:'start',target:'check',sourcePort:'success',targetPort:'in'},
    {id:'pass',source:'check',target:'done',sourcePort:'true',targetPort:'in'},
    {id:'fail',source:'check',target:'review',sourcePort:'false',targetPort:'in'},
  ],
  variables: [{name:'limit',type:'number',value:3}],
};
const run = {
  run:'run-1', workflow:'workflow-1', version:1, state:'failed', graph,
  steps:[{node:'start',status:'done',port:'success'},{node:'check',status:'done',port:'false'},
    {node:'done',status:'skipped'},{node:'review',status:'failed'}],
};

test('layout edits and reloaded layouts retain the original run and recorded steps', () => {
  const positioned = structuredClone(graph);
  positioned.nodes[0].position = {x:64,y:-16};
  positioned.annotations = [{id:'note',text:'Route review',position:{x:64,y:144}}];

  const draftOverlay = executionOverlay(run,run.run,run.workflow,positioned);
  const savedOverlay = executionOverlay(run,run.run,run.workflow,JSON.parse(JSON.stringify(positioned)));

  assert.strictEqual(draftOverlay,run);
  assert.strictEqual(savedOverlay,run);
  assert.equal(savedOverlay.version,1);
  assert.deepEqual(savedOverlay.steps.map(step=>step.status),['done','done','skipped','failed']);
  assert.deepEqual(savedOverlay.graph.nodes[0].position,{x:0,y:0});
});

test('executable edits hide the overlay and undo or redo follows the actual graph', () => {
  const moved = structuredClone(graph);
  moved.nodes[0].position.x = 300;
  const edited = structuredClone(moved);
  edited.nodes[1].config.rules[0].value = 8;

  for (const [view,expected] of [[moved,run],[edited,null],[moved,run],[edited,null]]) {
    assert.strictEqual(executionOverlay(run,run.run,run.workflow,view),expected);
  }
});

test('edge, scope, order, variable and configuration revision changes invalidate the projection', () => {
  const changes = [
    draft=>{draft.edges[1].sourcePort='error';},
    draft=>{draft.edges[1].target='review';},
    draft=>{draft.edges.pop();},
    draft=>{draft.edges.reverse();},
    draft=>{draft.nodes[1].parent='container';},
    draft=>{draft.nodes[1].configRevision=2;},
    draft=>{draft.variables[0].value=4;},
  ];
  for (const change of changes) {
    const draft = structuredClone(graph);
    change(draft);
    assert.equal(executionOverlay(run,run.run,run.workflow,draft),null);
  }
});

test('property order is flexible and the selected run and workflow must match', () => {
  const reordered = {edges:graph.edges,nodes:graph.nodes.map(node=>({config:node.config,...node})),variables:graph.variables};
  assert.strictEqual(executionOverlay(run,run.run,run.workflow,reordered),run);
  assert.equal(executionOverlay(run,'run-2',run.workflow,reordered),null);
  assert.equal(executionOverlay(run,run.run,'workflow-2',reordered),null);
  assert.equal(executionOverlay({...run,graph:null},run.run,run.workflow,reordered),null);
});
