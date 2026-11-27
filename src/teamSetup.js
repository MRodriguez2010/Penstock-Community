import {element as create, field, selectInput, textInput} from './studioFields.js';

export function mountTeamSetup({request, attempt, show, openRoles, openConnections, openNotifications}) {
  const root = create('section', undefined, 'connection-center');
  root.id = 'teamSetup';
  root.hidden = true;
  root.setAttribute('aria-labelledby', 'teamSetupTitle');
  const trigger = create('button', 'Team setup', 'studio-nav-link');
  trigger.id = 'openTeamSetup';
  trigger.type = 'button';
  trigger.hidden = true;
  document.querySelector('#workspaceNavigation .studio-nav-group').append(trigger);
  document.querySelector('#studio > .desk').append(root);

  const heading = create('h1', 'Team setup');
  heading.id = 'teamSetupTitle';
  heading.tabIndex = -1;
  const content = create('div');
  const feedback = create('p');
  feedback.setAttribute('role', 'status');
  root.append(heading, feedback, content);

  function action(label, run, disabled = false) {
    const button = create('button', label);
    button.type = 'button';
    button.disabled = disabled;
    button.onclick = () => attempt(run);
    return button;
  }

  function projectSteps(active) {
    const section = create('section');
    section.append(create('h2', 'Set up this project'));
    const items = create('ol');
    for (const [title, description, actions] of [
      ['Project roles and invitations', active ? 'Invite staff to the selected project and assign their roles.' :
        'Restart Penstock to unlock invitations for the selected project.', [action('Open project access', openRoles, !active)]],
      ['Connections', 'Review which services are available to the selected project.', [action('Open Connections', openConnections)]],
      ['Notifications', 'The project Inbox shows results and requests for review. External delivery is managed in Connections.',
        [action('Open Inbox', openNotifications), action('Manage delivery in Connections', openConnections)]],
    ]) {
      const item = create('li');
      item.append(create('h3', title), create('p', description), ...actions);
      items.append(item);
    }
    section.append(items);
    return section;
  }

  function settingsForm(current) {
    const form = create('form');
    const origin = textInput(current.origin, {type: 'url', required: true, maxLength: 1000});
    const method = selectInput([['local', 'Local invitations'], ['oidc', 'Organization sign-in (OIDC)']], 'local');
    const provider = create('fieldset');
    provider.append(create('legend', 'Identity provider'));
    const issuer = textInput('', {type: 'url', maxLength: 2000});
    const clientId = textInput('', {maxLength: 512});
    const keyEnv = textInput('', {maxLength: 128});
    keyEnv.pattern = '[A-Za-z_][A-Za-z0-9_]*';
    keyEnv.autocomplete = 'off';
    keyEnv.spellcheck = false;
    const callback = create('p');
    provider.append(field('Exact HTTPS issuer URL', issuer), field('Client ID', clientId),
      field('Client-secret environment variable name (optional)', keyEnv),
      create('p', 'For a confidential client, set the secret in the Penstock service environment before saving. Enter its variable name here.'), callback);
    function update() {
      provider.hidden = provider.disabled = method.value !== 'oidc';
      issuer.required = clientId.required = !provider.disabled;
      try {
        callback.textContent = `Register this redirect URI with your identity provider\n${new URL(origin.value).origin}/api/auth/oidc/callback`;
      } catch { callback.textContent = 'Enter the Penstock URL to see the identity provider redirect URI.'; }
    }
    method.onchange = origin.oninput = update;
    update();
    const save = create('button', 'Save settings for restart', 'primary');
    const expiry = create('p', 'After saving, restart the Penstock service from your hosting environment to activate team sign-in. Your owner key still opens the workspace.');
    form.append(field('Penstock URL staff will open', origin),
      create('p', 'Use the public HTTPS origin. HTTP is supported on loopback for local development.'),
      field('Staff sign-in method', method), provider, expiry, save);
    form.onsubmit = async event => {
      event.preventDefault();
      save.disabled = true;
      feedback.textContent = '';
      const body = {action: 'save', origin: origin.value, method: method.value};
      if (method.value === 'oidc') Object.assign(body, {issuer: issuer.value, clientId: clientId.value, keyEnv: keyEnv.value});
      try {
        render(await request('team-setup', body));
        if (!root.hidden) heading.focus();
      } catch (error) {
        feedback.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    };
    return form;
  }

  function render(current) {
    const method = current.method === 'oidc' ? 'Organization sign-in (OIDC)' : 'Local invitations';
    if (current.state === 'unconfigured') {
      content.replaceChildren(create('p', 'Choose how your staff will sign in to this self-hosted workspace.'),
        settingsForm(current), projectSteps(false));
      return;
    }
    const active = current.state === 'active';
    const state = create('h2', active ? 'Team sign-in is active' : current.activationError ? 'Team sign-in needs attention' :
      'Settings saved · Restart required');
    const help = create('p', active ? 'Use project invitations to grant individual access.' :
      'Restart the Penstock service from your hosting environment. Then open the address below with your owner key and return to Team setup.');
    const address = create('a', current.origin);
    address.href = current.origin;
    content.replaceChildren(state, help, create('p', method), address);
    if (current.activationError) content.append(create('p', current.activationError));
    if (current.redirectUri) {
      content.append(create('h3', 'Identity provider redirect URI'), create('p', current.redirectUri));
    }
    content.append(projectSteps(active));
  }

  trigger.onclick = () => attempt(async () => {
    show();
    feedback.textContent = '';
    content.replaceChildren(create('p', 'Loading team settings…'));
    heading.focus();
    try { render(await request('team-setup', {action: 'status'})); }
    catch (error) {
      content.replaceChildren();
      feedback.textContent = error.message;
    }
  });

  return {setAvailable(available) { trigger.hidden = !available; }};
}
