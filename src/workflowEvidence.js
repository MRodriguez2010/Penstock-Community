function sameGraphValue(current, recorded) {
  if (current === recorded) return true;
  if (!current || !recorded || typeof current !== 'object' || typeof recorded !== 'object') return false;
  if (Array.isArray(current) || Array.isArray(recorded)) {
    return Array.isArray(current) && Array.isArray(recorded) && current.length === recorded.length &&
      current.every((item, index) => sameGraphValue(item, recorded[index]));
  }
  const keys = Object.keys(current);
  return keys.length === Object.keys(recorded).length &&
    keys.every(key => Object.hasOwn(recorded, key) && sameGraphValue(current[key], recorded[key]));
}

function executableGraph({annotations, nodes, ...graph}) {
  return {...graph, nodes:nodes.map(({position, ...node}) => node)};
}

export function executionOverlay(trace, execution, workflow, graph) {
  if (!execution || trace?.run !== execution || trace.workflow !== workflow || !Array.isArray(trace.graph?.nodes)) return null;
  // Draft positions and canvas notes can change without changing the recorded execution.
  return sameGraphValue(executableGraph(graph), executableGraph(trace.graph)) ? trace : null;
}
