(() => {
  const sort = document.querySelector('[data-roster-sort]');
  if (sort) {
    sort.querySelector('select').addEventListener('change', () => sort.requestSubmit());
  }
  const filters = document.querySelector('.team-filters');
  if (!filters) return;
  const toggle = filters.querySelector('summary');
  document.addEventListener('click', event => {
    if (filters.open && !filters.contains(event.target)) filters.open = false;
  });
  document.addEventListener('keydown', event => {
    if (filters.open && event.key === 'Escape') {
      filters.open = false;
      toggle.focus();
    }
  });
  filters.querySelector('form').addEventListener('submit', () => { filters.open = false; });
  filters.querySelector('.filter-actions a').addEventListener('click', () => { filters.open = false; });
})();

(() => {
  const controls = document.querySelector('.team-view-switch');
  if (!controls) return;

  const buttons = controls.querySelectorAll('[data-team-view]');
  function renderView() {
    const url = new URL(window.location.href);
    const requested = url.searchParams.get('view');
    const legacyBooks = url.searchParams.has('books_page') || url.hash === '#team-books';
    const selected = requested === 'roster' || requested === 'books'
      ? requested : (legacyBooks ? 'books' : 'roster');

    for (const button of buttons) {
      const active = button.dataset.teamView === selected;
      button.setAttribute('aria-pressed', String(active));
      document.getElementById(button.getAttribute('aria-controls')).hidden = !active;
    }
  }

  for (const button of buttons) {
    button.addEventListener('click', () => {
      const url = new URL(window.location.href);
      url.searchParams.set('view', button.dataset.teamView);
      if (url.hash === '#team-books') url.hash = '';
      window.history.replaceState(window.history.state, '', url);
      renderView();
    });
  }

  renderView();
  controls.hidden = false;
  window.addEventListener('popstate', renderView);
  window.addEventListener('hashchange', renderView);
})();
