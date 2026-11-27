const ticketKey = 'penstock.office-signin-ticket';
const validTicket = /^[A-Za-z0-9_-]{43}$/;
const providers = new Set(['accounts.google.com', 'login.microsoftonline.com']);

export async function beginOfficeSignIn(request, project, kind) {
  const response = await request('ops-oauth-start', {project, kind, consent: true});
  const target = new URL(response.url);
  if (target.protocol !== 'https:' || target.port || target.username || target.password || !providers.has(target.hostname)) {
    throw new Error('The connection did not return a recognized provider sign-in page.');
  }
  window.location.assign(target.href);
}

function takeRedirect() {
  const fragment = window.location.hash.slice(1);
  if (fragment === 'office-oauth-error') {
    sessionStorage.removeItem(ticketKey);
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
    return 'error';
  }
  if (fragment.startsWith('office-oauth=')) {
    const candidate = new URLSearchParams(fragment).get('office-oauth');
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
    if (!validTicket.test(candidate || '')) return 'error';
    sessionStorage.setItem(ticketKey, candidate);
  }
  return sessionStorage.getItem(ticketKey);
}

function installRecovery() {
  let ticket = takeRedirect();
  if (!ticket) return;
  const studio = document.getElementById('studio');
  const header = document.querySelector('header');
  if (!studio || !header) return;
  const banner = document.createElement('section');
  banner.className = 'office-signin-banner';
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.textContent = 'Resume sign-in';
  retry.hidden = true;
  const close = document.createElement('button');
  close.type = 'button';
  close.textContent = 'Dismiss';
  let busy = false;
  banner.append(status, retry, close);
  header.after(banner);

  function forget() {
    ticket = null;
    sessionStorage.removeItem(ticketKey);
    observer.disconnect();
  }

  const observer = new MutationObserver(() => {
    if (!studio.hidden) void resume();
  });
  close.addEventListener('click', () => {
    forget();
    banner.remove();
  });
  retry.addEventListener('click', () => void resume());

  async function resume() {
    if (!ticket || ticket === 'error' || busy || studio.hidden) return;
    busy = true;
    retry.hidden = true;
    status.textContent = 'Completing the project connection…';
    try {
      const headers = {'Content-Type': 'application/json'};
      const ownerKey = sessionStorage.getItem('penstock.key');
      if (ownerKey) {
        headers.Authorization = `Bearer ${ownerKey}`;
      } else {
        const identity = await fetch('/api/auth/status', {cache: 'no-store'});
        const session = await identity.json();
        if (!session.user || !session.csrf) {
          status.textContent = 'Sign in as the project administrator who started this connection.';
          return;
        }
        headers['X-CSRF-Token'] = session.csrf;
      }
      const response = await fetch('/api/ops-oauth-finish', {method: 'POST', headers, cache: 'no-store',
        body: JSON.stringify({ticket})});
      const result = await response.json();
      if (!response.ok) {
        status.textContent = result.error || 'The connection could not be completed.';
        if (response.status === 401 || response.status === 403) retry.hidden = false;
        else forget();
        return;
      }
      forget();
      status.textContent = 'Project connection ready. Open Connections for the next step.';
      window.dispatchEvent(new CustomEvent('penstock:office-signin', {detail: result}));
    } catch {
      status.textContent = 'Penstock could not confirm the connection. Try to resume or check Connections.';
      retry.hidden = false;
    } finally {
      busy = false;
    }
  }

  if (ticket === 'error') {
    status.textContent = 'The sign-in response could not be resumed. Start again from Connections.';
    return;
  }
  status.textContent = 'Sign in as the project administrator who started this connection.';
  observer.observe(studio, {attributes: true, attributeFilter: ['hidden']});
  if (!studio.hidden) void resume();
}

if (typeof document !== 'undefined') installRecovery();
