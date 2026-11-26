const ORIGIN = 60;
const POSITION_LIMIT = 100000;
const NEARBY_GAP = 220;
const GAP_VARIATION = 24;
const CLEANUP_MOVE_LIMIT = 64;
const EPSILON = 0.000001;
const CLEANUP_AXES = [['x', 'y', 'height', CLEANUP_MOVE_LIMIT], ['y', 'x', 'width', 28]];

function measuredBox(item, measurements, fallbackWidth, fallbackHeight) {
  const {width, height} = measurements[item.id] || {};
  return {
    id: item.id,
    ...item.position,
    width: Number.isFinite(width) && width > 0 ? width : fallbackWidth,
    height: Number.isFinite(height) && height > 0 ? height : fallbackHeight,
  };
}

function graphOrder(nodes, edges, outputs) {
  const outgoing = new Map(nodes.map(({id}) => [id, []]));
  const incoming = new Map(nodes.map(({id}) => [id, 0]));
  for (const edge of edges) {
    outgoing.get(edge.source).push(edge);
    incoming.set(edge.target, incoming.get(edge.target) + 1);
  }
  for (const [id, connections] of outgoing) {
    const order = outputs.get(id) || [];
    const portOrder = new Map(order.map((port, index) => [port, index]));
    connections.sort((a, b) => (portOrder.get(a.sourcePort) ?? order.length) - (portOrder.get(b.sourcePort) ?? order.length));
  }

  const order = [], rank = new Map(nodes.map(({id}) => [id, 0]));
  while (incoming.size) {
    // A provisional cycle can exist in an unsaved graph. Break it in saved order.
    const id = [...incoming].find(([, count]) => count === 0)?.[0] ?? incoming.keys().next().value;
    incoming.delete(id);
    order.push(id);
    for (const {target} of outgoing.get(id)) {
      if (!incoming.has(target)) continue;
      incoming.set(target, incoming.get(target) - 1);
      rank.set(target, Math.max(rank.get(target), rank.get(id) + 1));
    }
  }

  const lane = new Map(), index = new Map(order.map((id, i) => [id, i]));
  let nextLane = 0;
  function followBranch(id, row) {
    lane.set(id, row);
    let continues = true;
    for (const {target} of outgoing.get(id)) {
      if (index.get(target) <= index.get(id) || lane.has(target)) continue;
      followBranch(target, continues ? row : nextLane++);
      continues = false;
    }
  }
  for (const id of order) if (!lane.has(id)) followBranch(id, nextLane++);
  return {rank, lane};
}

function offsets(sizes, gap, minimumStep = 0) {
  let at = ORIGIN;
  return sizes.map(size => {
    const start = at;
    at += Math.max(size + gap, minimumStep);
    return start;
  });
}

function grid(boxes, slots, columnGap, rowGap, minimumColumn = 0, minimumRow = 0) {
  const widths = [], heights = [];
  for (const [index, {column, row}] of slots.entries()) {
    widths[column] = Math.max(widths[column] || 0, boxes[index].width);
    heights[row] = Math.max(heights[row] || 0, boxes[index].height);
  }
  const xs = offsets(widths, columnGap, minimumColumn), ys = offsets(heights, rowGap, minimumRow);
  return boxes.map((box, i) => ({...box, x: xs[slots[i].column], y: ys[slots[i].row]}));
}

function layered(boxes, {rank, lane}, mode) {
  const byLane = mode === 'human';
  const vertical = mode.startsWith('vertical');
  const reversed = mode.endsWith('-rl') || mode.endsWith('-bt');
  const last = Math.max(...rank.values());
  const counts = new Map();
  const slots = boxes.map(({id}) => {
    const level = rank.get(id);
    const side = byLane ? lane.get(id) : (counts.get(level) || 0);
    counts.set(level, side + 1);
    const along = reversed ? last - level : level;
    return vertical ? {column: side, row: along} : {column: along, row: side};
  });
  if (mode === 'efficient') return grid(boxes, slots, 73, 32, 265, 160);
  return grid(boxes, slots, vertical ? 44 : 110, vertical ? 96 : 42);
}

function rectangular(boxes) {
  const columns = Math.ceil(Math.sqrt(boxes.length));
  return grid(boxes, boxes.map((_, i) => ({column: i % columns, row: Math.floor(i / columns)})), 62, 48);
}

function circular(boxes) {
  if (boxes.length === 1) return [{...boxes[0], x: ORIGIN, y: ORIGIN}];
  const points = boxes.map((_, i) => {
    const angle = 2 * Math.PI * i / boxes.length - Math.PI / 2;
    return {x: Math.cos(angle), y: Math.sin(angle)};
  });
  let radius = 0;
  for (const [i, a] of boxes.entries()) {
    for (let j = i + 1; j < boxes.length; j++) {
      const b = boxes[j];
      const separateX = ((a.width + b.width) / 2 + 52) / Math.abs(points[i].x - points[j].x);
      const separateY = ((a.height + b.height) / 2 + 52) / Math.abs(points[i].y - points[j].y);
      radius = Math.max(radius, Math.min(separateX, separateY));
    }
  }
  const positioned = boxes.map((box, i) => ({...box,
    x: Math.round(radius * points[i].x - box.width / 2),
    y: Math.round(radius * points[i].y - box.height / 2),
  }));
  const minX = Math.min(...positioned.map(({x}) => x)), minY = Math.min(...positioned.map(({y}) => y));
  return positioned.map(box => ({...box, x: ORIGIN + box.x - minX, y: ORIGIN + box.y - minY}));
}

const overlapsAxis = (a, b, axis, size, gap) => a[axis] < b[axis] + b[size] + gap && b[axis] < a[axis] + a[size] + gap;
const overlaps = (a, b, gap = 0) => overlapsAxis(a, b, 'x', 'width', gap) && overlapsAxis(a, b, 'y', 'height', gap);

// Open intervals leave exact edge contact available as a candidate.
function nearestOutside(value, intervals, low = -Infinity, high = Infinity) {
  intervals.sort(([a], [b]) => a - b);
  let start = -Infinity, end = -Infinity;
  for (const [left, right] of intervals) {
    if (left < end) end = Math.max(end, right);
    else if (value <= end) break;
    else [start, end] = [left, right];
  }
  if (value <= start || value >= end) return value;
  if (start < low) return end <= high ? end : null;
  if (end > high) return start;
  return value - start <= end - value ? start : end;
}

function avoidNotes(boxes, notes) {
  const gap = 24;
  function requiredShift(axis, size, across, acrossSize) {
    const blocked = [];
    for (const box of boxes) for (const note of notes) {
      if (overlapsAxis(box, note, across, acrossSize, gap)) {
        blocked.push([note[axis] - box[axis] - box[size] - gap, note[axis] + note[size] + gap - box[axis]]);
      }
    }
    const starts = boxes.map(box => box[axis]);
    return nearestOutside(0, blocked, -POSITION_LIMIT - Math.min(...starts), POSITION_LIMIT - Math.max(...starts));
  }
  const dx = requiredShift('x', 'width', 'y', 'height');
  const dy = requiredShift('y', 'height', 'x', 'width');
  if (dx === null && dy === null) return resolveOverlaps(boxes, notes);
  return dy === null || dx !== null && Math.abs(dx) <= Math.abs(dy)
    ? boxes.map(box => ({...box, x: box.x + dx}))
    : boxes.map(box => ({...box, y: box.y + dy}));
}

function resolveOverlaps(boxes, notes) {
  const placed = [...boxes];
  for (let i = placed.length - 1; i >= 0; i--) {
    const box = placed[i], obstacles = [...notes, ...placed.filter((_, j) => j !== i)];
    if (!obstacles.some(other => overlaps(box, other))) continue;

    const gap = 12;
    const horizontal = new Set([box.x]);
    for (const other of obstacles) {
      horizontal.add(other.x - box.width - gap);
      horizontal.add(other.x + other.width + gap);
    }
    let best = box, distance = Infinity;
    for (const x of horizontal) {
      if (Math.abs(x) > POSITION_LIMIT || Math.abs(x - box.x) >= distance) continue;
      const candidate = {...box, x};
      const blocked = obstacles.filter(other => overlapsAxis(candidate, other, 'x', 'width', gap))
        .map(other => [other.y - box.height - gap, other.y + other.height + gap]);
      const y = nearestOutside(box.y, blocked, -POSITION_LIMIT, POSITION_LIMIT);
      if (y === null) continue;
      const change = Math.hypot(x - box.x, y - box.y);
      if (change < distance) {
        best = {...candidate, y};
        distance = change;
      }
    }
    placed[i] = best;
  }
  return placed;
}

function cleanupRelationships(boxes, edges) {
  const related = new Map(boxes.map(({id}) => [id, new Set()]));
  const families = new Map(boxes.map(({id}) => [id, new Set()]));
  const children = new Map(boxes.map(({id}) => [id, []]));
  const parents = new Map(boxes.map(({id}) => [id, []]));
  function connect(groups, a, b) {
    groups.get(a).add(b);
    groups.get(b).add(a);
  }
  for (const {source, target} of edges) {
    children.get(source).push(target);
    parents.get(target).push(source);
    connect(related, source, target);
    connect(families, source, target);
  }
  for (const siblings of children.values()) {
    siblings.forEach((id, i) => {
      for (const peer of siblings.slice(i + 1)) {
        connect(related, id, peer);
        connect(families, id, peer);
      }
    });
  }
  for (const inputs of parents.values()) {
    inputs.forEach((id, i) => {
      for (const peer of inputs.slice(i + 1)) connect(related, id, peer);
    });
  }
  return {related, families};
}

function gapBetween(a, b, axis, size) {
  return a[axis] < b[axis] ? b[axis] - a[axis] - a[size] : a[axis] - b[axis] - b[size];
}

function cleanupScopes(boxes, related) {
  const scopes = [];
  const remaining = new Set(boxes);
  // Keep moves that can interact together, so a fallback affects only nearby blocks.
  const reach = 2 * CLEANUP_MOVE_LIMIT;
  function interacts(a, b) {
    return overlaps(a, b, reach) || related.get(a.id).has(b.id) && CLEANUP_AXES.some(([axis, along, size, tolerance]) =>
      Math.abs(a[axis] - b[axis]) <= tolerance + reach && gapBetween(a, b, along, size) <= NEARBY_GAP + reach);
  }
  for (const box of boxes) {
    if (!remaining.delete(box)) continue;
    const scope = [box];
    for (const member of scope) {
      for (const candidate of remaining) {
        if (!interacts(member, candidate)) continue;
        scope.push(candidate);
        remaining.delete(candidate);
      }
    }
    scopes.push(scope);
  }
  return scopes;
}

function railGroups(boxes, related, axis, along, size, tolerance) {
  const ordered = [...boxes].sort((a, b) => a[axis] - b[axis] || a[along] - b[along]);
  const assigned = new Set();
  const groups = [];
  for (const anchor of ordered) {
    if (assigned.has(anchor.id)) continue;
    const group = [anchor];
    assigned.add(anchor.id);
    const nearby = ordered.filter(box => !assigned.has(box.id) && box[axis] - anchor[axis] <= tolerance);
    for (const member of group) {
      for (const box of nearby) {
        if (assigned.has(box.id) || !related.get(member.id).has(box.id)) continue;
        const gap = gapBetween(member, box, along, size);
        if (gap < 0 || gap > NEARBY_GAP) continue;
        group.push(box);
        assigned.add(box.id);
      }
    }
    if (group.length > 1) groups.push(group);
  }
  return groups;
}

function medianRail(group, axis) {
  const coordinates = group.map(box => box[axis]).sort((a, b) => a - b);
  const low = coordinates[Math.floor((coordinates.length - 1) / 2)];
  const high = coordinates[Math.floor(coordinates.length / 2)];
  if (low === high) return low;
  const lowCount = coordinates.filter(value => value === low).length;
  const highCount = coordinates.filter(value => value === high).length;
  if (lowCount !== highCount) return lowCount > highCount ? low : high;
  return (low + high) / 2;
}

function placeClear(positions, candidates, notes) {
  const moved = new Set(candidates.map(({id}) => id));
  const obstacles = [...notes, ...[...positions.values()].filter(box => !moved.has(box.id))];
  for (const [i, candidate] of candidates.entries()) {
    if (obstacles.some(other => overlaps(candidate, other)) || candidates.slice(0, i).some(other => overlaps(candidate, other))) return false;
  }
  for (const candidate of candidates) positions.set(candidate.id, candidate);
  return true;
}

function regularizeGaps(group, families, axis, size, positions, notes) {
  const ordered = [...group].sort((a, b) => a[axis] - b[axis]);
  const gaps = ordered.slice(1).map((box, i) => {
    const previous = ordered[i];
    const gap = box[axis] - previous[axis] - previous[size];
    return families.get(box.id).has(previous.id) && gap >= 0 && gap <= NEARBY_GAP ? gap : null;
  });
  for (let start = 0; start < gaps.length;) {
    if (gaps[start] === null) {
      start++;
      continue;
    }
    let end = start + 1, low = gaps[start], high = gaps[start];
    while (end < gaps.length && gaps[end] !== null) {
      const nextLow = Math.min(low, gaps[end]), nextHigh = Math.max(high, gaps[end]);
      if (nextHigh - nextLow > GAP_VARIATION) break;
      [low, high] = [nextLow, nextHigh];
      end++;
    }
    if (end - start > 1 && high - low > EPSILON) {
      const spacing = gaps.slice(start, end).reduce((sum, gap) => sum + gap, 0) / (end - start);
      let at = ordered[start][axis];
      const candidates = ordered.slice(start + 1, end).map((box, i) => {
        at += ordered[start + i][size] + spacing;
        return {...box, [axis]: Math.abs(at - box[axis]) <= EPSILON ? box[axis] : at};
      });
      if (candidates.every((box, i) => Math.abs(box[axis] - ordered[start + i + 1][axis]) <= GAP_VARIATION)) {
        placeClear(positions, candidates, notes);
      }
    }
    start = end;
  }
}

function cleanRails(boxes, notes, {related, families}) {
  const positions = new Map(boxes.map(box => [box.id, box]));
  for (const [axis, along, size, tolerance] of CLEANUP_AXES) {
    for (const group of railGroups([...positions.values()], related, axis, along, size, tolerance)) {
      const rail = medianRail(group, axis);
      const aligned = group.map(box => ({...box, [axis]: rail}));
      if (placeClear(positions, aligned, notes)) regularizeGaps(aligned, families, along, size, positions, notes);
    }
  }
  return [...positions.values()];
}

function settleRails(boxes, obstacles, relationships) {
  // A row can make a lane newly eligible. Accept the local cleanup only once
  // another pass leaves it unchanged, within the original movement budget.
  let placed = boxes;
  for (let pass = 0; pass < 6; pass++) {
    const next = cleanRails(placed, obstacles, relationships);
    if (next.some((box, i) => Math.hypot(box.x - boxes[i].x, box.y - boxes[i].y) > CLEANUP_MOVE_LIMIT)) break;
    if (next.every((box, i) => box.x === placed[i].x && box.y === placed[i].y)) return next;
    placed = next;
  }
  return boxes;
}

function closestCleanup(boxes, notes, edges) {
  const separated = resolveOverlaps(boxes, notes);
  const relationships = cleanupRelationships(separated, edges);
  const placed = new Map(separated.map(box => [box.id, box]));
  for (const scope of cleanupScopes(separated, relationships.related)) {
    if (scope.length < 2) continue;
    const members = new Set(scope.map(({id}) => id));
    const obstacles = [...notes, ...[...placed.values()].filter(box => !members.has(box.id))];
    for (const box of settleRails(scope, obstacles, relationships)) placed.set(box.id, box);
  }
  return [...placed.values()];
}

function crosses(a, b, c, d) {
  const side = (start, end, p) => (end.x - start.x) * (p.y - start.y) - (end.y - start.y) * (p.x - start.x);
  return side(a, b, c) * side(a, b, d) < 0 && side(c, d, a) * side(c, d, b) < 0;
}

function passesThrough(from, to, box) {
  let start = 0, end = 1;
  for (const [axis, size] of [['x', 'width'], ['y', 'height']]) {
    const step = to[axis] - from[axis];
    const low = box[axis] - from[axis], high = low + box[size];
    if (step === 0) {
      if (low >= 0 || high <= 0) return false;
      continue;
    }
    start = Math.max(start, Math.min(low / step, high / step));
    end = Math.min(end, Math.max(low / step, high / step));
  }
  return start < end;
}

function layoutScore(boxes, edges) {
  const lookup = new Map(boxes.map(box => [box.id, box]));
  const lines = edges.map(edge => {
    const from = lookup.get(edge.source), to = lookup.get(edge.target);
    return {
      from: {x: from.x + from.width, y: from.y + from.height / 2},
      to: {x: to.x, y: to.y + to.height / 2},
      edge,
    };
  });
  const width = Math.max(...boxes.map(box => box.x + box.width)) - Math.min(...boxes.map(box => box.x));
  const height = Math.max(...boxes.map(box => box.y + box.height)) - Math.min(...boxes.map(box => box.y));
  let score = 4 * Math.max(width / 960, height / 640);
  for (const [i, {from, to, edge}] of lines.entries()) {
    score += Math.hypot(to.x - from.x, to.y - from.y) / 220 + (from.x > to.x ? 2 : 0);
    for (const box of boxes) {
      if (box.id !== edge.source && box.id !== edge.target && passesThrough(from, to, box)) score += 6;
    }
    for (let j = i + 1; j < lines.length; j++) {
      const other = lines[j];
      if (edge.source !== other.edge.source && edge.target !== other.edge.target && crosses(from, to, other.from, other.to)) score += 8;
    }
  }
  return score;
}

export function arrangeWorkflow({nodes, edges = [], notes = [], measurements = {}, outputs = new Map(), mode}) {
  if (!nodes.length) return new Map();
  const blocks = nodes.map(node => measuredBox(node, measurements, 192, 66 + 24 * Math.max(1, outputs.get(node.id)?.length || 0)));
  const obstacles = notes.map(note => measuredBox(note, measurements, 204, 104));
  const ids = new Set(blocks.map(({id}) => id));
  const connections = edges.filter(edge => ids.has(edge.source) && ids.has(edge.target));
  if (mode === 'closest') return new Map(closestCleanup(blocks, obstacles, connections).map(({id, x, y}) => [id, {x, y}]));

  const order = graphOrder(blocks, connections, outputs);
  const readingOrder = [...blocks].sort((a, b) => order.rank.get(a.id) - order.rank.get(b.id) || order.lane.get(a.id) - order.lane.get(b.id));
  function layout(choice) {
    if (choice === 'box') return rectangular(readingOrder);
    if (choice === 'circular') return circular(readingOrder);
    return layered(choice === 'efficient' ? blocks : readingOrder, order, choice);
  }

  let placed;
  if (mode === 'original') {
    let bestScore = Infinity;
    for (const choice of ['human', 'efficient', 'vertical-tb', 'box', 'circular']) {
      const candidate = layout(choice), score = layoutScore(candidate, connections);
      if (score < bestScore) {
        placed = candidate;
        bestScore = score;
      }
    }
  } else {
    placed = layout(mode);
  }
  if (obstacles.length) placed = avoidNotes(placed, obstacles);
  return new Map(placed.map(({id, x, y}) => [id, {x, y}]));
}
