(() => {
  const root = document.querySelector('[data-discord-tabs]');
  if (!root) return;
  const tabs = [...root.querySelectorAll('[data-discord-tab]')];
  function select(tab, focus = false, updateUrl = true) {
    tabs.forEach(button => {
      const active = button === tab;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
      document.getElementById(button.getAttribute('aria-controls')).hidden = !active;
    });
    if (focus) tab.focus();
    if (updateUrl) {
      const url = new URL(window.location.href);
      url.searchParams.set('destination', tab.dataset.discordTab);
      window.history.replaceState(null, '', url);
    }
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next === undefined) return;
      event.preventDefault();
      select(tabs[next], true);
    });
  });
  select(tabs.find(tab => tab.getAttribute('aria-selected') === 'true') || tabs[0], false, false);
  // Keep all controls in the DOM while hidden. Never store webhook input values
  // in a URL or browser storage. Warn only when navigation could discard edits.
  const dirty = new Set();
  const serialize = form => JSON.stringify([...new FormData(form)].filter(([key]) => !['csrfmiddlewaretoken','request_token'].includes(key)));
  root.querySelectorAll('form').forEach(form => {
    const initial = serialize(form);
    const invalid = form.dataset.formErrors === 'true';
    if (invalid) dirty.add(form);
    const changed = () => { if (invalid || serialize(form) !== initial) dirty.add(form); else dirty.delete(form); };
    form.addEventListener('input', changed);
    form.addEventListener('change', changed);
    form.addEventListener('submit', () => dirty.delete(form));
  });
  window.addEventListener('beforeunload', event => {
    if (!dirty.size) return;
    event.preventDefault();
    event.returnValue = '';
  });
})();
