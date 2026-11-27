const roles = ['viewer', 'reviewer', 'editor', 'executive', 'operator', 'admin'];

function element(tag, text) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  return node;
}

function labeled(label, control) {
  const node = element('label', label);
  node.append(control);
  return node;
}

function choices(values) {
  const select = element('select');
  select.replaceChildren(...values.map(value => new Option(value, value)));
  return select;
}

function field(type, autocomplete) {
  const input = element('input');
  input.type = type;
  input.autocomplete = autocomplete;
  input.required = true;
  return input;
}

export function mountManagedAccess({api, onSession, notice}) {
  const state = {enabled: false, invitations: false, oidc: false, user: null, csrf: null};
  const login = document.getElementById('login');
  const panel = element('section');
  const identity = element('span');
  const admin = element('details');
  const content = element('div');
  const heading = element('summary', 'Project access');
  const feedback = element('p');
  let activeProject;
  let invite;
  let inviteMethod;
  let refreshVersion = 0;
  feedback.setAttribute('role', 'status');
  panel.hidden = identity.hidden = admin.hidden = true;
  login?.prepend(panel);
  document.querySelector('header')?.append(identity);
  document.querySelector('#studio nav')?.append(admin);
  admin.append(heading, feedback, content);

  async function request(path, data) {
    const options = {cache: 'no-store', credentials: 'same-origin'};
    if (data !== undefined) {
      options.method = 'POST';
      options.headers = {'Content-Type': 'application/json'};
      if (state.csrf) options.headers['X-CSRF-Token'] = state.csrf;
      options.body = JSON.stringify(data);
    }
    const response = await fetch(`/api/auth/${path}`, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Sign-in request failed.');
    return result;
  }

  async function status() {
    Object.assign(state, await request('status'));
    identity.textContent = state.user?.email || state.user?.displayName || '';
    identity.hidden = !identity.textContent;
    return state;
  }

  async function authenticated() {
    await status();
    if (state.user) await onSession();
  }

  function action(label, run, report = notice) {
    const button = element('button', label);
    button.type = 'button';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await run(); }
      catch (error) { report(error.message); }
      finally { button.disabled = false; }
    });
    return button;
  }

  async function oidc() {
    const result = await request('oidc/start', invite ? {token: invite} : {});
    window.location.assign(result.url);
  }

  function buildLogin() {
    panel.replaceChildren();
    panel.hidden = !state.invitations;
    if (panel.hidden) return;
    if (invite && inviteMethod === 'oidc') {
      panel.append(element('h2', 'Accept your project invitation'),
        element('p', 'Use the organization account named in the invitation.'), action('Continue with organization sign-in', oidc));
      return;
    }
    const form = element('form');
    const email = field('email', 'username');
    const password = field('password', invite ? 'new-password' : 'current-password');
    password.maxLength = 256;
    const submit = element('button', invite ? 'Accept invitation' : 'Sign in');
    submit.className = 'primary';
    form.append(element('h2', invite ? 'Accept your project invitation' : 'Sign in to Penstock'));
    if (!invite) form.append(labeled('Email address', email));
    else form.append(element('p', 'Create a password with at least 15 characters. If you already have a Penstock account, use its password.'));
    form.append(labeled('Password', password), submit);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      submit.disabled = true;
      try {
        if (invite) await request('accept', {token: invite, password: password.value});
        else await request('login', {email: email.value, password: password.value});
        password.value = '';
        invite = undefined;
        await authenticated();
      } catch (error) { notice(error.message); }
      finally { submit.disabled = false; }
    });
    panel.append(form);
    if (!invite && state.oidc) panel.append(action('Use organization sign-in', oidc));
  }

  async function refreshMembers() {
    const project = activeProject;
    const version = ++refreshVersion;
    const result = await api('managed/members', {project});
    if (version !== refreshVersion || project !== activeProject) return;
    const report = text => feedback.textContent = text;
    const form = element('form');
    const email = field('email', 'email');
    const role = choices(roles);
    const method = choices(result.oidc ? ['local', 'oidc'] : ['local']);
    const subject = field('text', 'off');
    subject.required = false;
    subject.maxLength = 255;
    const subjectLabel = labeled('OIDC subject from administrator records (optional)', subject);
    subjectLabel.hidden = true;
    method.addEventListener('change', () => subjectLabel.hidden = method.value !== 'oidc');
    const issue = element('button', 'Create invitation link');
    const link = field('text', 'off');
    link.readOnly = true;
    link.required = false;
    const linkLabel = labeled('Private one-time invitation link', link);
    linkLabel.hidden = true;
    form.append(labeled('Invite by email address', email), labeled('Project role', role),
      labeled('Sign-in method', method), subjectLabel, issue, linkLabel);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      issue.disabled = true;
      try {
        const created = await api('managed/invite', {project, email: email.value, role: role.value,
          method: method.value, subject: method.value === 'oidc' && subject.value ? subject.value : undefined});
        if (version !== refreshVersion || project !== activeProject) return;
        link.value = created.url;
        linkLabel.hidden = false;
        link.select();
        report('Share this link privately with the intended recipient. It expires in 48 hours.');
        memberList.replaceChildren(...renderMembers(await api('managed/members', {project})));
      } catch (error) { report(error.message); }
      finally { issue.disabled = false; }
    });
    function renderMembers(data) {
      const output = [element('h3', 'Members')];
      for (const person of data.members) {
        const item = element('div');
        const select = choices(roles);
        select.value = person.role;
        item.append(element('p', `${person.email} · ${person.method}`), labeled('Role', select),
          action('Save role', async () => {
            await api('managed/member', {project, id: person.id, role: select.value});
            report('Project role updated.');
            await refreshMembers();
          }, report),
          action('Remove access', async () => {
            if (!window.confirm(`Remove ${person.email} from this project?`)) return;
            await api('managed/revoke', {project, id: person.id});
            report('Project access removed.');
            await refreshMembers();
          }, report));
        output.push(item);
      }
      if (!data.members.length) output.push(element('p', 'No managed members.'));
      if (data.invites.length) output.push(element('h3', 'Pending invitations'));
      for (const person of data.invites) {
        const item = element('div');
        item.append(element('p', `${person.email} · ${person.role}`), action('Revoke invitation', async () => {
          await api('managed/revoke', {project, email: person.email});
          report('Invitation revoked.');
          await refreshMembers();
        }, report));
        output.push(item);
      }
      return output;
    }
    const memberList = element('div');
    memberList.replaceChildren(...renderMembers(result));
    content.replaceChildren(form, memberList);
  }

  admin.addEventListener('toggle', () => {
    if (admin.open && activeProject) refreshMembers().catch(error => feedback.textContent = error.message);
  });

  function renderProject(catalog) {
    const capabilities = catalog.managed?.capabilities;
    if (!capabilities) return;
    const allowed = new Set(capabilities);
    if (state.user) {
      identity.textContent = `${state.user.email || state.user.displayName} · ${catalog.managed.role}`;
      identity.hidden = false;
    }
    for (const [id, permission] of Object.entries({new: 'edit', edit: 'edit', test: 'test', run: 'live',
      publish: 'publish', addProject: 'owner', armTrigger: 'owner'})) {
      const button = document.getElementById(id);
      if (button) button.hidden = !allowed.has(permission);
    }
    const project = allowed.has('admin') ? catalog.project : undefined;
    if (activeProject !== project) {
      activeProject = project;
      refreshVersion += 1;
      feedback.textContent = '';
      content.replaceChildren();
      if (admin.open && project) refreshMembers().catch(error => feedback.textContent = error.message);
    }
    admin.hidden = !project || !state.invitations;
  }

  async function restore({activate = true} = {}) {
    const hash = window.location.hash.slice(1);
    if (hash === 'sign-in-error') {
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
      notice('Sign-in could not be completed. Start again or ask your project administrator for a new link.');
    } else if (hash.startsWith('invite=')) {
      const params = new URLSearchParams(hash);
      invite = params.get('invite');
      inviteMethod = params.get('method');
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    }
    try {
      await status();
      buildLogin();
      if (state.user && !invite && activate) await onSession();
    } catch (error) { notice(error.message); }
  }

  return {
    get csrf() { return state.csrf; }, get user() { return state.user; },
    restore, renderProject,
    openProjectAccess() {
      if (admin.hidden) return;
      admin.open = true;
      heading.focus();
    },
    async logout() { await request('logout', {}); state.user = state.csrf = null; },
  };
}
