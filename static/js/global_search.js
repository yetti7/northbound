(() => {
  const dialog = document.querySelector('[data-global-search]');
  if (!dialog) return;
  const trigger = document.querySelector('[data-open-search]');
  const input = dialog.querySelector('input');
  const results = dialog.querySelector('[data-search-results]');
  const status = dialog.querySelector('[data-search-status]');
  let timer;
  let controller;

  function open() {
    if (dialog.open) return;
    dialog.showModal();
    input.focus();
  }

  trigger.addEventListener('click', open);
  dialog.querySelector('[data-close-search]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => trigger.focus());
  document.addEventListener('keydown', event => {
    if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'k') return;
    if (event.target.closest?.('input, textarea, select, [contenteditable]')) return;
    event.preventDefault();
    open();
  });

  input.addEventListener('input', () => {
    clearTimeout(timer);
    controller?.abort();
    results.replaceChildren();
    const query = input.value.trim();
    status.textContent = query.length < 2 ? 'Enter at least two characters.' : 'Searching…';
    if (query.length < 2) return;

    timer = setTimeout(async () => {
      const pending = new AbortController();
      controller = pending;
      const url = new URL(dialog.dataset.url, location.href);
      url.search = new URLSearchParams({q: query, group: dialog.dataset.group, month: dialog.dataset.month});
      try {
        const response = await fetch(url, {signal: pending.signal});
        if (!response.ok) throw new Error('Search unavailable');
        const data = await response.json();
        if (pending.signal.aborted) return;
        for (const group of data.results) {
          const section = document.createElement('section');
          const title = document.createElement('h3');
          title.textContent = group.category;
          section.append(title);
          for (const item of group.items) {
            const link = document.createElement('a');
            link.href = item.url;
            link.textContent = item.label;
            if (item.context) {
              const context = document.createElement('small');
              context.textContent = item.context;
              link.append(context);
            }
            section.append(link);
          }
          results.append(section);
        }
        status.textContent = data.results.length ? '' : 'No matches found.';
      } catch (error) {
        if (!pending.signal.aborted) status.textContent = 'Search unavailable. Try again.';
      }
    }, 220);
  });

  dialog.addEventListener('keydown', event => {
    const links = [...results.querySelectorAll('a')];
    if (!links.length) return;
    const index = links.indexOf(document.activeElement);
    if (event.key === 'Enter' && document.activeElement === input) {
      event.preventDefault();
      links[0].click();
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    const next = event.key === 'ArrowDown' ? index + 1 : index - 1;
    (index < 0 && event.key === 'ArrowUp' ? links.at(-1) : links[next] || input).focus();
  });
})();
