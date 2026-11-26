// Canvas coordinates only. The workflow editor uses right-side outputs and left-side inputs.
const clearance = 16;
const bendCost = 22;
const distance = (a, b) => Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
const point = (x, y) => ({x, y});
const same = (a, b) => a.x === b.x && a.y === b.y;
const expand = (r, gap) => ({...r, left:r.left-gap, right:r.right+gap, top:r.top-gap, bottom:r.bottom+gap});

function pipeLanes(edges) {
  const lanes = new Map(edges.map(e => [e.id, {}]));
  for (const [side, other] of [['source','target'], ['target','source']]) {
    const groups = new Map();
    for (const edge of edges) {
      const id = edge[side+'Id'];
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(edge);
    }
    for (const group of groups.values()) {
      group.sort((a,b) => a[other].y-b[other].y || a[other].x-b[other].x || a.id.localeCompare(b.id));
      group.forEach((e, i) => {
        const lane = lanes.get(e.id);
        lane[side] = i;
        lane[side+'Count'] = group.length;
      });
    }
  }
  return lanes;
}

// Open interiors let pipes follow an obstacle's padded boundary or pass a measured gap exactly.
function crosses(a, b, r) {
  if (a.y === b.y) return a.y > r.top && a.y < r.bottom && Math.min(a.x,b.x) < r.right && Math.max(a.x,b.x) > r.left;
  return a.x > r.left && a.x < r.right && Math.min(a.y,b.y) < r.bottom && Math.max(a.y,b.y) > r.top;
}

function simplify(points) {
  const result = [];
  for (const p of points) {
    const a = result.at(-2), b = result.at(-1);
    if (b && same(b,p)) continue;
    if (a && (a.x === b.x && b.x === p.x && (b.y-a.y)*(p.y-b.y) > 0 ||
              a.y === b.y && b.y === p.y && (b.x-a.x)*(p.x-b.x) > 0)) result.pop();
    result.push(p);
  }
  return result;
}

// The most common three-segment pipe needs no search. Other simple templates handle return paths
// around the source and target, and detours around an intervening card or annotation.
function sharedLength(a,b,used) {
  const horizontal = a.y === b.y;
  const lo = horizontal ? Math.min(a.x,b.x) : Math.min(a.y,b.y);
  const hi = horizontal ? Math.max(a.x,b.x) : Math.max(a.y,b.y);
  let shared = 0;
  for (const segment of used) {
    if (horizontal !== segment.horizontal || Math.abs(segment.line - (horizontal ? a.y : a.x)) >= 5) continue;
    shared += Math.max(0, Math.min(hi,segment.hi) - Math.max(lo,segment.lo) - 8);
  }
  return shared;
}

function simpleRoute(start, end, boxes, laneShift, used) {
  let best = null;
  function consider(points) {
    const path = simplify(points);
    let shared = 0, length = 0;
    for (let i = 1; i < path.length; i++) {
      const a = path[i-1], b = path[i];
      if (boxes.some(r => crosses(a,b,r))) return false;
      shared += sharedLength(a,b,used);
      length += distance(a,b);
    }
    const score = length + bendCost*(path.length-2) + 4*shared;
    if (!best || score < best.score) best = {path,score};
    return !shared;
  }
  if (start.x <= end.x) {
    for (const offset of [0,12,-12,24,-24]) {
      const x = Math.max(start.x, Math.min(end.x, (start.x+end.x)/2 + laneShift + offset));
      if (consider([start,point(x,start.y),point(x,end.y),end])) return best.path;
      if (!used.length) break;
    }
  }
  const left = Math.min(start.x,end.x), right = Math.max(start.x,end.x);
  const top = Math.min(start.y,end.y), bottom = Math.max(start.y,end.y);
  const nearby = boxes.filter(r => r.left <= right && r.right >= left && r.top <= bottom && r.bottom >= top);
  const ys = new Set([start.y,end.y,...nearby.flatMap(r => [r.top,r.bottom])]);
  for (const segment of used) {
    if (!segment.horizontal || segment.hi < left || segment.lo > right || segment.line < top-80 || segment.line > bottom+80) continue;
    ys.add(segment.line-12);
    ys.add(segment.line+12);
  }
  const ordered = [...ys].sort((a,b) => Math.abs(start.y-a)+Math.abs(end.y-a) - Math.abs(start.y-b)-Math.abs(end.y-b));
  for (const y of ordered) {
    // A return leg must turn clear of both handles before heading backwards.
    if (start.x > end.x && (y === start.y || y === end.y)) continue;
    if (consider([start,point(start.x,y),point(end.x,y),end])) break;
  }
  return best?.path;
}

// A* on the obstacle-side coordinate lines. Only reachable intersections are visited. The axis
// is part of the search state so paths with fewer bends can win without losing an open corridor.
function searchRoute(start, end, boxes) {
  const coords = values => [...new Set(values)].sort((a,b) => a-b);
  const xs = coords([start.x,end.x,...boxes.flatMap(r => [r.left,r.right])]);
  const ys = coords([start.y,end.y,...boxes.flatMap(r => [r.top,r.bottom])]);
  const columns = new Map(), rows = new Map();
  function blocked(a,b,horizontal) {
    const cache = horizontal ? rows : columns, key = horizontal ? a.y : a.x;
    if (!cache.has(key)) cache.set(key, boxes.filter(r => horizontal ? key > r.top && key < r.bottom : key > r.left && key < r.right));
    return cache.get(key).some(r => crosses(a,b,r));
  }
  const heap = [], best = new Map();
  function push(entry) {
    let i = heap.length;
    heap.push(entry);
    while (i) {
      const parent = (i-1) >> 1;
      if (heap[parent].priority <= entry.priority) break;
      heap[i] = heap[parent];
      i = parent;
    }
    heap[i] = entry;
  }
  function pop() {
    const first = heap[0], last = heap.pop();
    if (!heap.length) return first;
    let i = 0;
    while (i*2+1 < heap.length) {
      let child = i*2+1;
      if (child+1 < heap.length && heap[child+1].priority < heap[child].priority) child++;
      if (last.priority <= heap[child].priority) break;
      heap[i] = heap[child];
      i = child;
    }
    heap[i] = last;
    return first;
  }
  function visit(ix,iy,axis,cost,prev) {
    const key = (iy*xs.length+ix)*2+axis;
    if (cost >= (best.get(key) ?? Infinity)) return;
    best.set(key,cost);
    const p = point(xs[ix],ys[iy]);
    push({ix,iy,axis,cost,prev,key,p,priority:cost+distance(p,end)});
  }
  visit(xs.indexOf(start.x),ys.indexOf(start.y),0,0,null);
  while (heap.length) {
    const cur = pop();
    if (cur.cost !== best.get(cur.key)) continue;
    if (same(cur.p,end)) {
      const path = [];
      for (let item = cur; item; item = item.prev) path.push(item.p);
      return path.reverse();
    }
    for (const [dx,dy] of [[1,0],[0,-1],[0,1],[-1,0]]) {
      const ix = cur.ix+dx, iy = cur.iy+dy;
      if (ix < 0 || ix >= xs.length || iy < 0 || iy >= ys.length) continue;
      const p = point(xs[ix],ys[iy]), axis = dy ? 1 : 0;
      if (dx < 0 && (!cur.prev || same(p,end)) || blocked(cur.p,p,axis === 0)) continue;
      visit(ix,iy,axis,cur.cost+distance(cur.p,p)+(axis === cur.axis ? 0 : bendCost),cur);
    }
  }
  return null;
}

function escapeHandle(port, own, direction, gap, track) {
  const side = own ? (direction === 1 ? own.right : own.left) : port.x;
  return point(port.x + direction * (Math.max(1, (side-port.x)*direction) + gap + track), port.y);
}

function finishRoute(vertices,rectangles) {
  const points = simplify(vertices);
  const radii = points.map((b,i) => {
    const a = points[i-1], c = points[i+1];
    if (!a || !c) return 0;
    const dx = Math.sign(a.x-b.x) || Math.sign(c.x-b.x);
    const dy = Math.sign(a.y-b.y) || Math.sign(c.y-b.y);
    if (!dx || !dy) return 0;
    let radius = Math.min(76,distance(a,b)/2,distance(b,c)/2);
    for (const r of rectangles) {
      // Coordinate distances u,v inside this corner's turning quadrant satisfy
      // sqrt(u) + sqrt(v) = sqrt(radius) on the quadratic. The nearest rectangle
      // corner therefore bounds the entire continuous curve, including its stroke gutter.
      if (dx > 0 ? r.right+2 <= b.x : r.left-2 >= b.x) continue;
      if (dy > 0 ? r.bottom+2 <= b.y : r.top-2 >= b.y) continue;
      const u = Math.max(0, dx > 0 ? r.left-2-b.x : b.x-r.right-2);
      const v = Math.max(0, dy > 0 ? r.top-2-b.y : b.y-r.bottom-2);
      radius = Math.min(radius,(Math.sqrt(u)+Math.sqrt(v))**2);
    }
    return radius;
  });
  return {points,radii};
}

export function routePipe({source, target, sourceId, targetId, rectangles, lane = {}, used = []}) {
  const sourceBox = rectangles.find(r => r.id === sourceId), targetBox = rectangles.find(r => r.id === targetId);
  const sourceLane = lane.source || 0, targetLane = lane.target || 0;
  const laneShift = ((sourceLane - ((lane.sourceCount || 1)-1)/2) - (targetLane - ((lane.targetCount || 1)-1)/2)) * 10;
  // Narrow gaps can accommodate the pipe even when the normal gutter around two cards overlaps.
  for (const gap of [clearance, 4, 0]) {
    const boxes = rectangles.map(r => expand(r,gap));
    const track = gap === clearance ? 7 : 0;
    const start = escapeHandle(source,sourceBox,1,gap,Math.min(6,sourceLane)*track);
    const end = escapeHandle(target,targetBox,-1,gap,Math.min(6,targetLane)*track);
    if (boxes.some(r => r.id !== sourceId && crosses(source,start,r) || r.id !== targetId && crosses(end,target,r))) continue;
    const path = simpleRoute(start,end,boxes,laneShift,used) || searchRoute(start,end,boxes);
    if (path) return finishRoute([source,...path,target],rectangles);
  }
  // A user can place a note directly over a handle. Follow only the occluded portion to its
  // visible boundary, then route the rest around every card as usual.
  function exposed(port,dir,own) {
    const p = escapeHandle(port,own,dir,1,0);
    let x;
    do {
      x = p.x;
      for (const r of rectangles) {
        if (port.y <= r.top || port.y >= r.bottom) continue;
        if (dir === 1 && r.left <= p.x && r.right > port.x) p.x = Math.max(p.x,r.right+1);
        if (dir === -1 && r.right >= p.x && r.left < port.x) p.x = Math.min(p.x,r.left-1);
      }
    } while (p.x !== x);
    return p;
  }
  const start = exposed(source,1,sourceBox), end = exposed(target,-1,targetBox);
  const path = simpleRoute(start,end,rectangles,laneShift,used) || searchRoute(start,end,rectangles);
  return finishRoute([source,...(path || [start,point(start.x,end.y),end]),target],rectangles);
}

export function pipePath(points, radii) {
  let path = `M ${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length; i++) {
    const a = points[i-1], b = points[i], c = points[i+1];
    const r = radii?.[i];
    if (!c || !r) { path += ` L ${b.x} ${b.y}`; continue; }
    const x1 = b.x+Math.sign(a.x-b.x)*r, y1 = b.y+Math.sign(a.y-b.y)*r;
    const x2 = b.x+Math.sign(c.x-b.x)*r, y2 = b.y+Math.sign(c.y-b.y)*r;
    path += ` L ${x1} ${y1} Q ${b.x} ${b.y} ${x2} ${y2}`;
  }
  return path;
}

// Project each label's footprint onto the segments. Prefer an open stretch near the middle,
// or near the destination after a fan-out, so a label doesn't sit beneath an unrelated node.
function pipeLabel(points,rectangles,width,fanout,radii) {
  const total = points.slice(1).reduce((sum,p,i) => sum+distance(points[i],p),0);
  const preferred = total*(fanout ? .67 : .5);
  let best = null, traveled = 0;
  for (let i = 1; i < points.length; i++) {
    const a = points[i-1], b = points[i], horizontal = a.y === b.y;
    const start = horizontal ? a.x : a.y, finish = horizontal ? b.x : b.y;
    const direction = Math.sign(finish-start), length = Math.abs(finish-start);
    const ideal = start + direction*(preferred-traveled);
    const from = start+direction*(radii[i-1] || 0), to = finish-direction*(radii[i] || 0);
    const lo = Math.min(from,to), hi = Math.max(from,to);
    const excluded = rectangles.flatMap(r => {
      if (horizontal && a.y > r.top-10 && a.y < r.bottom+10) return [[r.left-width/2,r.right+width/2]];
      if (!horizontal && a.x > r.left-width/2 && a.x < r.right+width/2) return [[r.top-10,r.bottom+10]];
      return [];
    }).sort((a,b) => a[0]-b[0]);
    function consider(low,high) {
      if (low > high) return;
      const position = Math.min(high, Math.max(low,ideal));
      const score = Math.abs(position-ideal) + (horizontal ? 0 : 8);
      if (!best || score < best.score) best = {score, at:horizontal ? point(position,a.y) : point(a.x,position)};
    }
    let low = lo;
    for (const [from,to] of excluded) {
      if (to <= low) continue;
      if (from >= hi) break;
      if (low <= from) consider(low,from);
      low = Math.max(low,to);
    }
    consider(low,hi);
    traveled += length;
  }
  return best?.at;
}

export function planPipes(rectangles,edges) {
  const lanes = pipeLanes(edges), used = [], labelBoxes = [...rectangles], plans = new Map();
  for (const edge of edges) {
    const lane = lanes.get(edge.id);
    const route = routePipe({...edge,rectangles,lane,used});
    for (let i = 1; i < route.points.length; i++) {
      const a = route.points[i-1], b = route.points[i], horizontal = a.y === b.y;
      const lo = horizontal ? Math.min(a.x,b.x) : Math.min(a.y,b.y);
      const hi = horizontal ? Math.max(a.x,b.x) : Math.max(a.y,b.y);
      used.push({horizontal,lo,hi,line:horizontal ? a.y : a.x});
    }
    const width = edge.labelWidth;
    if (width) {
      const fanout = lane.sourceCount > 1;
      route.label = pipeLabel(route.points,labelBoxes,width,fanout,route.radii);
      if (!route.label) {
        route.label = pipeLabel(route.points,labelBoxes,width,fanout,[]) || route.points[Math.floor(route.points.length/2)];
        // Keep the label on the stroke in both styles even when its only free spot is near a bend.
        for (let i = 1; i < route.points.length; i++) {
          const a = route.points[i-1], b = route.points[i];
          if (distance(a,route.label)+distance(route.label,b) !== distance(a,b)) continue;
          route.radii[i-1] = Math.min(route.radii[i-1],distance(a,route.label));
          route.radii[i] = Math.min(route.radii[i],distance(b,route.label));
          break;
        }
      }
      const {x,y} = route.label;
      labelBoxes.push({left:x-width/2, right:x+width/2, top:y-10, bottom:y+10});
    }
    plans.set(edge.id,route);
  }
  return plans;
}
