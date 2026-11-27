// Read only the signed-in project's durable inbox. The server owns read receipts.
export function watchInbox({request, open, project, count, popup, activeReference, attempt}) {
  let selected, previous, sequence = 0, timer;

  function close() {
    popup.hidden = true;
    popup.replaceChildren();
  }
  async function check() {
    const id = project();
    const checkId = ++sequence;
    clearTimeout(timer);
    if (!id || document.hidden) return;
    try {
      const {items} = await request('ops-notifications', {project: id});
      if (checkId !== sequence || id !== project()) return;
      const unseen = items.filter(item => !item.seen);
      count.textContent = unseen.length ? String(unseen.length) : '';
      count.hidden = !unseen.length;
      const item = previous && unseen.find(e => !previous.has(e.id) &&
        ['waiting', 'failed', 'changes-requested', 'interrupted'].includes(e.state) &&
        e.reference.split('/')[0] !== activeReference());
      previous = new Set(items.map(e => e.id));
      if (item && popup.hidden) {
        const heading = document.createElement('strong');
        heading.textContent = {waiting:'Approval needed', failed:'Run needs attention',
          'changes-requested':'Changes requested', interrupted:'Run interrupted'}[item.state];
        const text = document.createElement('span');
        text.textContent = item.title;
        const result = document.createElement('button');
        result.type = 'button';
        result.textContent = 'Open result';
        result.onclick = () => attempt(async () => {
          close();
          if (id !== project()) return;
          await open(item.reference);
          await request('ops-notification-ack', {project:id, id:item.id});
          await check();
        });
        const dismiss = document.createElement('button');
        dismiss.type = 'button';
        dismiss.textContent = 'Dismiss';
        dismiss.onclick = close;
        popup.replaceChildren(heading, text, result, dismiss);
        popup.hidden = false;
      }
    } catch {
      // Transient connectivity leaves the existing inbox and any open decision intact.
    } finally {
      if (checkId === sequence && id === project() && !document.hidden) timer = setTimeout(check, 20000);
    }
  }
  function select(id) {
    if (selected === id) return;
    selected = id;
    previous = undefined;
    count.hidden = true;
    close();
    void check();
  }
  document.addEventListener('visibilitychange', check);
  window.addEventListener('pagehide', () => {++sequence; clearTimeout(timer); close();});
  window.addEventListener('pageshow', e => {if (e.persisted) void check();});
  return {select, check};
}
