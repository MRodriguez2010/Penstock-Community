export function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

export function labelFor(key) {
  const names = {after: 'After', slots: 'Slots', id: 'ID', api: 'API', url: 'URL', input: 'Input', output: 'Output',
    expectedSha256: 'Expected SHA-256', expectedBytes: 'Expected size in bytes', latestOnly: 'Latest version of each file'};
  const text = String(key).replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[-_]/g, ' ');
  return names[key] ?? text.charAt(0).toUpperCase() + text.slice(1);
}

export function field(label, control) {
  const node = element('label', label);
  node.append(control);
  return node;
}

export function textInput(value = '', {type = 'text', required = false, maxLength, rows} = {}) {
  const node = element(rows ? 'textarea' : 'input');
  if (rows) node.rows = rows;
  else node.type = type;
  node.required = required;
  if (maxLength) node.maxLength = maxLength;
  node.value = value ?? '';
  return node;
}

export function selectInput(options, value) {
  const node = element('select');
  for (const [key, label] of options) node.add(new Option(label, key, false, key === value));
  return node;
}

export function requestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
}

export function createDraftRecovery(storage, identity, project, role) {
  const owner = 'penstock.draft.v1/' + encodeURIComponent(identity) + '/';
  const scope = owner + encodeURIComponent(project) + '/' + encodeURIComponent(role) + '/';
  const keyFor = copy => scope + encodeURIComponent(copy.workflow || 'unsaved') + '/' +
    (copy.baseVersion ?? 0) + '/' + encodeURIComponent(copy.id);
  const keys = prefix => Array.from({length: storage.length}, (_, i) => storage.key(i))
    .filter(key => key?.startsWith(prefix));
  return {
    list() {
      return keys(scope).flatMap(key => {
        try {
          const saved = JSON.parse(storage.getItem(key));
          if (typeof saved?.id === 'string' && saved.id && key === keyFor(saved) && Number.isFinite(saved.at) &&
              typeof saved.brief === 'string' && (saved.workflow === null || typeof saved.workflow === 'string') &&
              (saved.baseVersion === null || Number.isInteger(saved.baseVersion))) return [saved];
        } catch {
          // A damaged copy can be removed with the rest of this account's local drafts.
        }
        return [];
      }).sort((left, right) => right.at - left.at);
    },
    write(copy) {
      const encoded = JSON.stringify(copy);
      if (encoded.length > 400000) throw Error('The working draft is too large for local recovery.');
      storage.setItem(keyFor(copy), encoded);
    },
    discard(id) {
      for (const key of keys(scope)) if (key.endsWith('/' + encodeURIComponent(id))) storage.removeItem(key);
    },
    clearAccount() {for (const key of keys(owner)) storage.removeItem(key);},
  };
}

export function draftRecoveryStatus(copy, versions) {
  if (!copy.workflow) return 'current';
  const related = versions.filter(version => version.workflow === copy.workflow);
  if (!related.length || related.some(version => version.archived)) return 'unavailable';
  const latest = related.reduce((head, version) => Math.max(head, version.version), 0);
  return latest === copy.baseVersion ? 'current' : 'newer';
}

function protectedField(key, value) {
  const name = String(key).replace(/[^a-z0-9]/gi, '').toLowerCase();
  if (/^(?:authorization|cookie|setcookie|credentials?|privatekey|apikey|keyenv|password|clientsecret|secret|secretkey|(?:access|refresh|auth|session|bearer|csrf)?token)$/.test(name)) {
    return 'Protected value';
  }
  if (typeof value === 'string' && /^(?:sourcecode|script|program|command|code)$/.test(name) &&
      !/^[a-z0-9_./-]{1,32}$/i.test(value)) return 'Code hidden';
  return null;
}

export function readableText(value) {
  return String(value)
    .replace(/\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}/gi, '$1 [hidden]')
    .replace(/(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s&,;]+)/gi, '$1[hidden]')
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, '$1[hidden]@');
}

export function valueView(value, depth = 0) {
  if (value == null || value === '') return element('span', 'None', 'muted');
  if (typeof value === 'boolean') return element('span', value ? 'Yes' : 'No');
  if (typeof value !== 'object') return element('span', readableText(value), 'studio-literal');
  if (depth > 7) return element('span', 'Additional nested fields', 'muted');
  if (Array.isArray(value)) {
    if (!value.length) return element('span', 'No items', 'muted');
    const list = element('ul', null, 'studio-values-list');
    for (const item of value) {
      const row = element('li');
      row.append(valueView(item, depth + 1));
      list.append(row);
    }
    return list;
  }
  const entries = Object.entries(value);
  if (!entries.length) return element('span', 'No fields', 'muted');
  const details = element('dl', null, 'studio-values');
  for (const [key, item] of entries) {
    const row = element('div');
    const definition = element('dd');
    const hidden = protectedField(key, item);
    definition.append(hidden ? element('span', hidden, 'muted') : valueView(item, depth + 1));
    row.append(element('dt', labelFor(key)), definition);
    details.append(row);
  }
  return details;
}

export function resultView(value) {
  if (typeof value !== 'string') return valueView(value);
  const text = value.trim();
  const fenced = /^```(?:json)?\s*([\s\S]*?)\s*```$/i.exec(text);
  const structured = fenced ? fenced[1] : text;
  if (structured.startsWith('{') || structured.startsWith('[')) {
    try {
      const parsed = JSON.parse(structured);
      if (parsed && typeof parsed === 'object') return valueView(parsed);
    } catch {
      // Ordinary text can start with a bracket. Render it as text.
    }
  }
  const body = element('div', null, 'studio-formatted-result');
  let list;
  let fence = false;
  for (const line of value.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) {
      if (!fence) body.append(element('p', 'This result includes a code sample.', 'muted'));
      fence = !fence;
      list = null;
      continue;
    }
    if (fence) continue;
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    const item = /^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/.exec(line);
    if (item) {
      const type = item[2] ? 'ol' : 'ul';
      if (list?.localName !== type) {
        list = element(type);
        body.append(list);
      }
      list.append(element('li', readableText(item[3])));
    } else {
      list = null;
      if (heading) body.append(element(heading[1].length < 3 ? 'h4' : 'h5', readableText(heading[2])));
      else if (line.trim()) body.append(element('p', readableText(line)));
    }
  }
  return body.childNodes.length ? body : valueView(value);
}

const VALUE_TYPES = [
  ['string', 'Text'], ['number', 'Number'], ['boolean', 'Yes or no'],
  ['object', 'Fields'], ['array', 'List'], ['null', 'Empty'],
];
const INITIAL = {string: '', number: 0, boolean: false, object: {}, array: [], null: null};

function valueType(value) {
  if (value == null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

export function valueEditor(value, {fixed = false, depth = 0, name = 'Value', recovered} = {}) {
  const wrap = element('div', null, 'studio-value-editor');
  const type = selectInput(VALUE_TYPES, recovered?.kind || valueType(value));
  type.setAttribute('aria-label', name + ' type');
  const control = element('div');
  if (!fixed) wrap.append(type);
  wrap.append(control);
  let read, snapshot;

  function render(next, state) {
    control.replaceChildren();
    const kind = state?.kind || valueType(next);
    if (kind === 'null') {
      read = () => null;
      snapshot = () => ({kind});
      control.append(element('p', 'Empty value', 'muted'));
      return;
    }
    if (kind === 'object' || kind === 'array') {
      if (depth >= 7) {
        control.append(valueView(next));
        read = () => structuredClone(next);
        snapshot = () => ({kind, original: structuredClone(next)});
        return;
      }
      const object = kind === 'object';
      const rows = [];
      const list = element('div', null, 'studio-value-rows');
      function add(key, item, saved) {
        const line = element('div', null, 'studio-value-row');
        const hidden = object && protectedField(key, item);
        const edit = hidden
          ? {node: element('p', hidden, 'muted'), read: () => item, snapshot: () => ({hidden: item})}
          : valueEditor(item, {depth: depth + 1, name: key || 'Item', recovered: saved});
        const input = object ? textInput(key, {maxLength: 128}) : null;
        if (input) {
          input.setAttribute('aria-label', 'Field name');
          input.placeholder = 'Field name';
          line.append(input);
        }
        const remove = element('button', 'Remove');
        remove.type = 'button';
        remove.setAttribute('aria-label', object && key ? 'Remove ' + key : 'Remove item');
        const entry = {input, edit};
        remove.onclick = () => {
          rows.splice(rows.indexOf(entry), 1);
          line.remove();
        };
        line.append(edit.node, remove);
        rows.push(entry);
        list.append(line);
      }
      if (state?.entries) {
        for (const [key, saved] of state.entries) {
          const original = next != null && Object.hasOwn(next, key) ? next[key] : undefined;
          const item = Object.hasOwn(saved, 'hidden') ? saved.hidden : saved.original ?? original;
          add(key, item, saved);
        }
      } else {
        for (const [key, item] of Object.entries(next)) add(key, item);
      }
      const append = element('button', object ? 'Add field' : 'Add item', 'studio-quiet');
      append.type = 'button';
      append.onclick = () => add('', '');
      control.append(list, append);
      read = () => {
        if (!object) return rows.map(({edit}) => edit.read());
        const entries = rows.map(({input, edit}) => [input.value.trim(), edit.read()]);
        if (entries.some(([key]) => !key) || new Set(entries.map(([key]) => key)).size !== entries.length) {
          throw Error('Give each field a distinct name.');
        }
        return Object.fromEntries(entries);
      };
      snapshot = () => ({kind, entries: rows.map(({input, edit}, index) => [input?.value ?? String(index), edit.snapshot()])});
      return;
    }
    let input;
    if (kind === 'boolean') {
      input = selectInput([['true', 'Yes'], ['false', 'No']], state?.raw ?? String(next));
      read = () => input.value === 'true';
    } else if (kind === 'number') {
      input = textInput(state?.raw ?? next, {type: 'number'});
      input.step = 'any';
      read = () => {
        if (!input.value || !Number.isFinite(input.valueAsNumber)) throw Error(name + ' needs a valid number.');
        return input.valueAsNumber;
      };
    } else {
      const raw = state?.raw ?? String(next ?? '');
      input = textInput(raw, raw.includes('\n') ? {rows: 3} : {});
      read = () => input.value;
    }
    input.setAttribute('aria-label', name);
    snapshot = () => ({kind, raw: input.value});
    control.append(input);
  }

  render(recovered?.original ?? value, recovered);
  type.onchange = () => render(INITIAL[type.value]);
  return {node: wrap, read: () => read(), snapshot: () => snapshot()};
}

export function inputEditor(value, fields = []) {
  if (!fields.length) return valueEditor(value, {name: 'Run input'});
  const node = element('div', null, 'studio-structured-fields');
  const inputs = fields.map(spec => {
    const initial = value?.[spec.name];
    const editor = valueEditor(initial ?? spec.value ?? INITIAL[spec.type],
      {fixed: true, name: labelFor(spec.name)});
    const row = element('div');
    row.append(element('strong', labelFor(spec.name)), editor.node);
    if (spec.description) row.append(element('small', spec.description, 'muted'));
    node.append(row);
    return [spec.name, editor];
  });
  return {
    node,
    read: () => Object.fromEntries(inputs.map(([name, editor]) => [name, editor.read()])),
  };
}

export const LOOKUP_BLOCKS = {
  'web-search': {label: 'Search the web', operations: [['search', 'Search the web']]},
  github: {label: 'Look up a repository', operations: [['repository', 'Repository details'], ['issues', 'List issues'], ['pull-requests', 'List pull requests']]},
  enrichment: {label: 'Look up a person or company', operations: [['person', 'Professional profile'], ['company', 'Company details']]},
  'service-health': {label: 'Check service health', operations: [['service-health', 'Read service health']]},
  'worker-status': {label: 'Check worker status', operations: [['worker-status', 'Read worker status']]},
  'published-workflow': {label: 'Look up a published workflow', operations: [['published-workflow', 'Read publication details']]},
  'weather-observation': {label: 'Read a weather observation', operations: [['weather-observation', 'Read station measurement']]},
};

const LOOKUP_INPUTS = {
  search: ['Search query', 'project reporting resources'],
  repository: ['Repository owner/name', 'example/repository', 1],
  issues: ['Repository owner/name', 'example/repository'],
  'pull-requests': ['Repository owner/name', 'example/repository'],
  person: ['Work email address', 'person@example.org'],
  company: ['Company domain', 'example.org'],
  'service-health': ['Service ID', 'example-service', 1],
  'worker-status': ['Worker ID', 'example-worker', 1],
  'published-workflow': ['Published workflow ID', 'example-workflow', 1],
  'weather-observation': ['Weather station ID', 'example-station', 1],
};

const EDITABLE_TYPES = [['review', 'Review decision'], ['condition', 'Text condition'],
  ['group-records', 'Group records'], ['ordered-condition', 'Ordered condition'],
  ['file', 'List project files'], ['verify-artifact', 'Verify project file'], ['response', 'Set result']];
const COMPARISONS = [['equals', 'Equals'], ['differs', 'Differs'], ['contains', 'Contains'],
  ['starts-with', 'Starts with'], ['gt', 'Greater than'], ['gte', 'Greater than or equal'],
  ['lt', 'Less than'], ['lte', 'Less than or equal']];

function orderedBranches(branches, recovered) {
  const node = element('div', null, 'studio-condition-branches');
  const editors = branches.map((branch, index) => {
    const prior = recovered?.[branch.id];
    const row = element('fieldset');
    row.append(element('legend', 'Branch ' + (index + 1)));
    const title = textInput(prior?.label ?? branch.label ?? branch.id, {required: true, maxLength: 80});
    const operator = selectInput(COMPARISONS, prior?.operator ?? branch.operator);
    const right = valueEditor(branch.right, {name: 'Compare with', recovered: prior?.right});
    const path = valueEditor(branch.path || [], {fixed: true, name: 'Input path', recovered: prior?.path});
    row.append(field('Branch name', title), field('Comparison', operator),
      element('strong', 'Compare with'), right.node, element('strong', 'Input path'), path.node);
    node.append(row);
    return {
      id: branch.id,
      read: () => {
        if (!title.value.trim()) throw Error('Give each branch a name.');
        return {...branch, label: title.value.trim(), operator: operator.value, right: right.read(), path: path.read()};
      },
      snapshot: () => ({label: title.value, operator: operator.value, right: right.snapshot(), path: path.snapshot()}),
    };
  });
  return {node, read: () => editors.map(editor => editor.read()),
    snapshot: () => Object.fromEntries(editors.map(editor => [editor.id, editor.snapshot()]))};
}

function namedValue(container, label, value, recovered) {
  const control = valueEditor(value, {fixed: true, name: label, recovered});
  const region = element('div', null, 'studio-file-setting');
  region.append(element('strong', label), control.node);
  container.append(region);
  return control;
}

function fileLookup(config, exact, recovered) {
  const node = element('div');
  const choose = selectInput([['name', 'File name'], ['id', 'Saved file ID']], recovered?.by || ('id' in config ? 'id' : 'name'));
  node.append(field('Find a file by', choose));
  const inputs = element('div');
  node.append(inputs);
  let read, snapshot;
  function render(saved = null) {
    inputs.replaceChildren();
    if (choose.value === 'id') {
      const id = namedValue(inputs, 'Saved file ID', config.id ?? '', saved?.id);
      read = () => ({id: id.read()});
      snapshot = () => ({by: 'id', id: id.snapshot()});
      return;
    }
    const name = namedValue(inputs, 'Saved file name', config.name ?? '', saved?.name);
    const useVersion = selectInput([['latest', 'Latest version'], ['exact', 'Exact version']],
      saved?.useVersion || ('version' in config ? 'exact' : 'latest'));
    const versionBox = element('div');
    if (!exact) inputs.append(field('Which version?', useVersion));
    inputs.append(versionBox);
    const version = namedValue(versionBox, 'Version number', config.version ?? 1, saved?.version);
    const sync = () => {versionBox.hidden = !exact && useVersion.value !== 'exact';};
    useVersion.onchange = sync;
    sync();
    read = () => ({name: name.read(), ...(versionBox.hidden ? {} : {version: version.read()})});
    snapshot = () => ({by: 'name', name: name.snapshot(), useVersion: useVersion.value, version: version.snapshot()});
  }
  choose.onchange = () => render();
  render(recovered);
  return {node, read: () => read(), snapshot: () => snapshot()};
}

function fileSettings(config, verify, recovered) {
  const node = element('div', null, 'studio-file-settings');
  if (verify) {
    const lookup = fileLookup(config, true, recovered?.lookup);
    node.append(lookup.node);
    const hash = namedValue(node, 'Expected SHA-256', config.expectedSha256, recovered?.hash);
    const bytes = namedValue(node, 'Expected size in bytes', config.expectedBytes, recovered?.bytes);
    return {node, read: () => ({...lookup.read(), expectedSha256: hash.read(), expectedBytes: bytes.read()}),
      snapshot: () => ({lookup: lookup.snapshot(), hash: hash.snapshot(), bytes: bytes.snapshot()})};
  }
  const selection = selectInput([['list', 'List project files'], ['read', 'Read text file'],
    ['save', 'Save text to project files']], recovered?.operation || config.operation);
  node.append(field('File operation', selection));
  const settings = element('div');
  node.append(settings);
  let read, snapshot;
  function render(saved = null) {
    settings.replaceChildren();
    switch (selection.value) {
      case 'read': {
        const lookup = fileLookup(config, false, saved?.lookup);
        settings.append(lookup.node);
        read = lookup.read;
        snapshot = () => ({lookup: lookup.snapshot()});
        break;
      }
      case 'save': {
        const name = namedValue(settings, 'File name', config.name ?? '', saved?.name);
        const source = selectInput([['input', 'Use the incoming text'], ['text', 'Enter text']],
          saved?.source || ('text' in config ? 'text' : 'input'));
        settings.append(field('Text source', source));
        const body = element('div');
        const control = namedValue(body, 'Text to save', config.text ?? '', saved?.text);
        settings.append(body);
        const sync = () => {body.hidden = source.value !== 'text';};
        source.onchange = sync;
        sync();
        read = () => ({name: name.read(), ...(body.hidden ? {} : {text: control.read()})});
        snapshot = () => ({name: name.snapshot(), source: source.value, text: control.snapshot()});
        break;
      }
      default: {
        const search = namedValue(settings, 'File name contains', config.search ?? '', saved?.search);
        const limit = namedValue(settings, 'Maximum results', config.limit ?? 100, saved?.limit);
        const latest = namedValue(settings, 'Latest version of each file only', config.latestOnly ?? false, saved?.latest);
        read = () => ({search: search.read(), limit: limit.read(), latestOnly: latest.read()});
        snapshot = () => ({search: search.snapshot(), limit: limit.snapshot(), latest: latest.snapshot()});
        break;
      }
    }
  }
  selection.onchange = () => render();
  render(recovered);
  return {node, read: () => ({operation: selection.value, ...read()}),
    snapshot: () => ({operation: selection.value, ...snapshot()})};
}

function lookupSettings(kind, config, adapters, recovered) {
  const node = element('div');
  const options = Object.entries(adapters).filter(([, profile]) => profile.kind === kind);
  const ids = options.map(([id, profile]) => [id, `${id} · ${profile.host}`]);
  const configured = recovered?.adapter ?? config.adapter;
  if (!ids.some(([id]) => id === configured)) ids.unshift([configured, `${configured} · Unavailable`]);
  const adapter = selectInput(ids, configured);
  const operation = selectInput(LOOKUP_BLOCKS[kind].operations, recovered?.operation ?? config.operation);
  const reference = config.query?.ref;
  const referenceMode = reference && (reference.path || []).length === 0 && ['$input', '$previous'].includes(reference.node)
    ? reference.node : reference ? 'existing' : 'typed';
  const modes = [['typed', 'Enter a value'], ['$input', 'Use the workflow input'], ['$previous', 'Use the previous step result']];
  if (referenceMode === 'existing') modes.push(['existing', 'Keep the saved workflow field reference']);
  const mode = selectInput(modes, recovered?.mode ?? referenceMode);
  const query = textInput(recovered?.query ?? (typeof config.query === 'string' ? config.query : ''), {maxLength: 300});
  const queryLabel = field('', query);
  const queryTitle = element('span');
  queryLabel.prepend(queryTitle);
  const limit = textInput(recovered?.limit ?? config.limit, {type: 'number', required: true});
  limit.min = '1';
  limit.step = '1';
  const disclosure = element('p');
  function update() {
    const [label, example, maximum = 10] = LOOKUP_INPUTS[operation.value];
    queryTitle.textContent = label;
    query.placeholder = example;
    queryLabel.hidden = mode.value !== 'typed';
    query.required = !queryLabel.hidden;
    limit.max = String(maximum);
    if (maximum === 1) limit.value = '1';
    disclosure.textContent = Object.hasOwn(adapters, adapter.value)
      ? `Offline tests use sample results. A live run requires approval before sending this value to ${adapters[adapter.value].host}.`
      : 'Ask IT to install a lookup adapter for this project.';
  }
  adapter.onchange = operation.onchange = mode.onchange = update;
  update();
  node.append(field('Project lookup adapter', adapter), field('Lookup operation', operation), field('Input source', mode),
    queryLabel, field('Maximum results', limit), disclosure);
  return {
    node,
    read() {
      if (!Object.hasOwn(adapters, adapter.value)) throw Error('Choose an available lookup adapter.');
      if (!limit.checkValidity()) throw Error('Choose a result limit within the displayed range.');
      if (mode.value === 'typed' && !query.value.trim()) throw Error('Enter a value for the lookup.');
      return {adapter: adapter.value, operation: operation.value, limit: Number(limit.value),
        query: mode.value === 'typed' ? query.value.trim() : mode.value === 'existing' ? config.query : {ref: {node: mode.value}}};
    },
    snapshot: () => ({adapter: adapter.value, operation: operation.value, mode: mode.value, query: query.value, limit: limit.value}),
  };
}

export function planEditor(plan, attempt, {step, recovered, lookupAdapters = {}} = {}) {
  const draft = structuredClone(plan);
  const wrap = element('div', null, 'studio-plan');
  const name = textInput(recovered?.name ?? draft.name, {required: true, maxLength: 100});
  const stepList = element('ol', null, 'studio-plan-steps');
  const card = element('section', null, 'studio-plan-edit');
  const region = element('div', null, 'studio-plan-grid');
  region.append(stepList, card);
  wrap.append(field('Workflow name', name), region);
  let active = draft.steps.find(candidate => candidate.id === (recovered?.step ?? step)) || draft.steps[0];
  let pending = recovered?.step === active.id ? recovered.active : null;
  let readStep = () => {}, snapshotStep = () => ({});

  function update() {
    const prior = pending;
    pending = null;
    card.replaceChildren();
    stepList.replaceChildren(...draft.steps.map(step => {
      const item = element('li');
      const link = element('button', step.label || labelFor(step.type));
      link.type = 'button';
      link.setAttribute('aria-pressed', String(step === active));
      link.onclick = () => attempt(() => {
        readStep();
        active = step;
        update();
        card.querySelector('input')?.focus();
      });
      item.append(link);
      return item;
    }));
    const title = textInput(prior?.name ?? active.label ?? labelFor(active.id), {required: true, maxLength: 100});
    card.append(element('h2', LOOKUP_BLOCKS[active.type]?.label || labelFor(active.type)), field('Step name', title));
    const config = Object.entries(active.config || {});
    const file = ['file', 'verify-artifact'].includes(active.type)
      ? fileSettings(active.config, active.type === 'verify-artifact', prior?.file) : null;
    if (file) card.append(file.node);
    const lookup = Object.hasOwn(LOOKUP_BLOCKS, active.type)
      ? lookupSettings(active.type, active.config, lookupAdapters, prior?.lookup) : null;
    if (lookup) card.append(lookup.node);
    const editors = file || lookup ? [] : config.map(([key, value]) => {
      const hidden = protectedField(key, value);
      if (key === 'code' || hidden) {
        card.append(element('p', key === 'code' ? 'This step runs a workspace-configured function.' : labelFor(key) + ' · ' + hidden));
        return [key, () => value];
      }
      if (active.type === 'source-read' && ['source', 'binding'].includes(key)) {
        if (key === 'source') card.append(element('p', 'Source · ' + labelFor(value)));
        return [key, () => value];
      }
      const editor = active.type === 'ordered-condition' && key === 'branches'
        ? orderedBranches(value, prior?.config?.[key])
        : valueEditor(value, {fixed: true, name: labelFor(key), recovered: prior?.config?.[key]});
      const setting = element('div');
      setting.append(element('strong', labelFor(key)), editor.node);
      card.append(setting);
      return [key, editor.read, editor.snapshot];
    });
    const ports = typeof active.next === 'string' ? {success: active.next} : active.next || {};
    const edges = Object.entries(ports);
    const links = edges.map(([port, targets]) => {
      const destinations = [targets].flat().map((target, index) => {
        const choices = draft.steps.filter(step => step !== active && step.type !== 'start');
        const control = selectInput(choices.map(step => [step.id, step.label || labelFor(step.type)]),
          prior?.links?.[port]?.[index] ?? target);
        const branch = active.type === 'ordered-condition' && active.config.branches.find(item => item.id === port);
        card.append(field((branch?.label || labelFor(port)) + ([targets].flat().length > 1 ? ' ' + (index + 1) : '') + ' goes to', control));
        return control;
      });
      return [port, Array.isArray(targets), destinations];
    });
    readStep = () => {
      if (!title.value.trim()) throw Error('Give the step a name.');
      active.label = title.value.trim();
      if (file) active.config = file.read();
      else if (lookup) active.config = lookup.read();
      else if (editors.length) active.config = Object.fromEntries(editors.map(([key, read]) => [key, read()]));
      if (links.length) {
        const next = Object.fromEntries(links.map(([port, multi, destinations]) =>
          [port, multi ? destinations.map(control => control.value) : destinations[0].value]));
        active.next = typeof active.next === 'string' ? next.success : next;
      }
    };
    snapshotStep = () => ({
      name: title.value,
      config: Object.fromEntries(editors.filter(([, , snapshot]) => snapshot)
        .map(([key, , snapshot]) => [key, snapshot()])),
      file: file?.snapshot(),
      lookup: lookup?.snapshot(),
      links: Object.fromEntries(links.map(([port, , destinations]) => [port, destinations.map(control => control.value)])),
    });
    if (!edges.length) return;
    const available = edges.filter(([, target]) => typeof target === 'string');
    if (!available.length || draft.steps.length >= 40) return;
    const addControls = element('div', null, 'studio-plan-add');
    const route = selectInput(available.map(([port]) => [port, labelFor(port)]), available[0][0]);
    const lookupTypes = Object.entries(LOOKUP_BLOCKS).filter(([kind]) => Object.values(lookupAdapters).some(profile => profile.kind === kind));
    const availableTypes = [...EDITABLE_TYPES, ...lookupTypes.map(([kind, block]) => [kind, block.label])];
    const choose = selectInput(availableTypes, availableTypes[0][0]);
    const add = element('button', 'Insert step', 'studio-quiet');
    add.type = 'button';
    add.onclick = () => attempt(() => {
      readStep();
      const updatedPorts = typeof active.next === 'string' ? {success: active.next} : active.next;
      const next = updatedPorts[route.value];
      const kind = choose.value;
      const values = {
        review: {question: 'Approve this result?'},
        condition: {contains: 'Urgent'},
        'group-records': {field: 'status'},
        'ordered-condition': {branches: [{id: 'match', operator: 'equals', right: 'urgent'}]},
        file: {operation: 'list', search: '', limit: 50, latestOnly: true},
        'verify-artifact': {name: 'report.txt', version: 1, expectedSha256: '', expectedBytes: 0},
        response: {value: {ref: {node: '$previous'}}},
      };
      if (Object.hasOwn(LOOKUP_BLOCKS, kind)) {
        const operation = LOOKUP_BLOCKS[kind].operations[0][0];
        const adapter = Object.keys(lookupAdapters).find(name => lookupAdapters[name].kind === kind);
        const [, query, maximum = 10] = LOOKUP_INPUTS[operation];
        values[kind] = {adapter, operation, query, limit: Math.min(5, maximum)};
      }
      const outputs = {
        review: {approved: next, declined: next},
        condition: {true: next, false: next},
        'group-records': {success: next, error: next},
        'ordered-condition': {match: next, default: next, error: next},
        'verify-artifact': {success: next, error: next},
      };
      const step = {id: 'step-' + requestId(), type: kind, label: availableTypes.find(([type]) => type === kind)[1], config: values[kind],
        next: outputs[kind] || next};
      active.next = {...updatedPorts, [route.value]: step.id};
      draft.steps.splice(draft.steps.indexOf(active) + 1, 0, step);
      active = step;
      update();
      card.querySelector('input')?.focus();
    });
    addControls.append(field('On path', route), field('Add step', choose), add);
    card.append(addControls);
  }
  update();
  return {
    node: wrap,
    snapshot: () => ({plan: structuredClone(draft), name: name.value, step: active.id, active: snapshotStep()}),
    read: () => {
      readStep();
      if (!name.value.trim()) throw Error('Give the workflow a name.');
      draft.name = name.value.trim();
      return draft;
    },
  };
}
