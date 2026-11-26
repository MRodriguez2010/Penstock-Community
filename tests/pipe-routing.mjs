import assert from 'node:assert/strict';
import {test} from 'node:test';
import {pipePath,planPipes,routePipe} from '../src/pipeRouting.js';

const p = (x,y) => ({x,y});
const box = (id,left,top,width,height) => ({id,left,top,right:left+width,bottom:top+height});
const source = p(197,62), target = p(625,129);
const sides = [box('out',0,0,192,112),box('in',630,83,192,115)];
const edge = {id:'edge',sourceId:'out',targetId:'in',source,target,labelWidth:68};
const inside = (p,r) => p.x > r.left && p.x < r.right && p.y > r.top && p.y < r.bottom;
const segments = points => points.slice(1).map((b,i) => [points[i],b]);
function misses(points,r) {
  return segments(points).every(([a,b]) => {
    assert.ok(a.x === b.x || a.y === b.y, 'orthogonal between bends');
    if (a.x === b.x) return a.x <= r.left || a.x >= r.right || Math.min(a.y,b.y) >= r.bottom || Math.max(a.y,b.y) <= r.top;
    return a.y <= r.top || a.y >= r.bottom || Math.min(a.x,b.x) >= r.right || Math.max(a.x,b.x) <= r.left;
  });
}
function assertClear(route,boxes,input=edge) {
  const {points,radii} = route;
  assert.deepEqual(points[0],input.source);
  assert.deepEqual(points.at(-1),input.target);
  assert.ok(points[1].x > points[0].x && points[1].y === points[0].y);
  assert.ok(points.at(-2).x < points.at(-1).x && points.at(-2).y === points.at(-1).y);
  for (const r of boxes) assert.ok(misses(points,r), 'path crosses '+r.id);
  const d = pipePath(points,radii);
  assert.doesNotMatch(d,/NaN|Infinity/);
  // Sample each actual quadratic, including the portion which cuts inside a padded corner.
  const commands = [...d.matchAll(/([MLQ]) ([\d. -]+)/g)];
  let last = null;
  for (const [,op,args] of commands) {
    const [x,y,ex,ey] = args.trim().split(/\s+/).map(Number);
    if (op !== 'Q') { last=p(x,y); continue; }
    for (let t = 0; t <= 1; t += .05) {
      const at = p((1-t)**2*last.x+2*(1-t)*t*x+t*t*ex,(1-t)**2*last.y+2*(1-t)*t*y+t*t*ey);
      for (const r of boxes) assert.ok(!inside(at,r), 'bend enters '+r.id);
    }
    last=p(ex,ey);
  }
}

test('measured note, exact handles, label clearance, and manual drag in both pipe styles',() => {
  const note = box('note',264,-48,276,248), boxes = [...sides,note];
  const route = planPipes(boxes,[edge]).get(edge.id);
  assertClear(route,boxes);
  for (const r of boxes) {
    assert.ok(!inside(route.label,{left:r.left-34,right:r.right+34,top:r.top-10,bottom:r.bottom+10}));
  }
  assert.match(pipePath(route.points,route.radii),/ Q /);
  assert.doesNotMatch(pipePath(route.points,0),/ Q /);
  const moved = [...sides,box('note',264,400,276,248)];
  const updated = planPipes(moved,[edge]).get(edge.id);
  assertClear(updated,moved);
  assert.notDeepEqual(updated.points,route.points);
});

test('backward and loop routes steer around their own cards and an unrelated obstacle',() => {
  const backward = {...edge, source:p(827,140), target:p(-5,60), sourceId:'in',targetId:'out'};
  const boxes = [...sides,box('block',280,-45,200,275)];
  assertClear(routePipe({...backward,rectangles:boxes}),boxes,backward);
  const loop = {...edge,target:p(-5,89),targetId:'out'};
  assertClear(routePipe({...loop,rectangles:boxes}),boxes,loop);
});

test('multiple obstacles with alternating open corridors keep the closed sections clear',() => {
  const boxes = [...sides,box('wall1',240,-210,60,310),box('wall2',350,12,60,312),box('wall3',460,-215,60,310),box('wall4',545,10,60,312)];
  assertClear(routePipe({...edge,rectangles:boxes}),boxes);
});

test('a tight passage beside an anchor retries with reduced clearance',() => {
  const boxes = [...sides,box('close note',203,32,145,89)];
  const route = routePipe({...edge,rectangles:boxes});
  assertClear(route,boxes);
  assert.ok(route.radii[1] < 12);
});

test('shared output branches and independent routes keep their labels and major elbows apart',() => {
  const edges = [
    {...edge,id:'one',source:p(0,0),target:p(500,150)},
    {...edge,id:'two',sourceId:'out2',targetId:'in2',source:p(100,10),target:p(400,160)},
    {...edge,id:'three',source:p(0,0),targetId:'in3',target:p(430,-145)},
  ];
  const plans = planPipes([],edges);
  for (const e of edges) assertClear(plans.get(e.id),[],e);
  const lines = e => segments(plans.get(e).points).filter(([a,b]) => a.x === b.x && Math.abs(a.y-b.y)>100).map(([a]) => a.x);
  assert.ok(lines('one').every(x => !lines('two').includes(x)));
  const labels = [...plans.values()].map(s => s.label);
  assert.ok(labels.every((p,i) => labels.slice(i+1).every(q => Math.abs(p.x-q.x)>=68 || Math.abs(p.y-q.y)>=20)));
});


test('curved pipes use sweeping bends in open space while preserving right/left handle tangents',() => {
  const open = {...edge,target:p(810,380)};
  const boxes = [sides[0],box('in',815,306,192,112)];
  const route = routePipe({...open,rectangles:boxes});
  assertClear(route,boxes,open);
  assert.ok(Math.max(...route.radii) >= 64);
  assert.match(pipePath(route.points,route.radii),/^M 197 62 L [\d.]+ 62 Q/);
});
