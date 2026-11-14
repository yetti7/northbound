(() => {
  const search = document.querySelector('[data-participant-search]');
  if (!search) return;
  const input = search.querySelector('input');
  const clear = search.querySelector('[data-participant-clear]');
  const count = search.querySelector('[data-participant-count]');
  const empty = document.querySelector('[data-participant-empty]');
  const sort = document.querySelector('[data-roster-sort]');
  const remember = Boolean(sort?.dataset.searchOwner);
  const key = 'team-roster-search:' + sort?.dataset.searchOwner + ':' + location.pathname;
  if (remember) {
    try { input.value = sessionStorage.getItem(key) || ''; } catch { /* Storage is optional. */ }
  }
  const rows = [...document.querySelectorAll('[data-participant-result]')];

  function filter() {
    const query = input.value.trim().toLowerCase();
    let matches = 0;
    for (const row of rows) {
      const visible = [row.dataset.username, row.dataset.displayName, row.dataset.discord]
        .some(value => (value || '').toLowerCase().includes(query));
      row.hidden = !visible;
      if (visible && row.tagName === 'TR') matches++;
    }
    if (remember) {
      try { sessionStorage.setItem(key, input.value); } catch { /* Storage is optional. */ }
    }
    clear.disabled = !input.value;
    count.textContent = `${matches} ${matches === 1 ? 'participant' : 'participants'}`;
    empty.hidden = matches !== 0;
  }

  input.addEventListener('input', filter);
  clear.addEventListener('click', () => {
    input.value = '';
    filter();
    input.focus();
  });
  search.hidden = false;
  filter();
})();
