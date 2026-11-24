(() => {
  const sort = document.querySelector('[data-roster-sort]');
  if (sort) {
    sort.querySelector('select').addEventListener('change', () => sort.requestSubmit());
  }
  const filters = document.querySelector('.team-filters');
  if (!filters) return;
  const toggle = filters.querySelector('summary');
  const panel = filters.querySelector('.team-filter-panel');
  const desktop = window.matchMedia('(min-width: 801px)');
  function positionFilters() {
    panel.style.removeProperty('left');
    panel.style.removeProperty('top');
    panel.style.removeProperty('max-height');
    if (!filters.open || !desktop.matches) return;
    const margin = 16;
    const anchor = toggle.getBoundingClientRect();
    if (anchor.bottom < margin || anchor.top > innerHeight - margin) {
      filters.open = false;
      return;
    }
    const width = panel.getBoundingClientRect().width;
    const left = Math.max(margin, Math.min(anchor.left, innerWidth - width - margin));
    const below = Math.max(0, innerHeight - anchor.bottom - margin - 8);
    const above = Math.max(0, anchor.top - margin - 8);
    const naturalHeight = panel.scrollHeight + 2;
    const openAbove = naturalHeight > below && above > below;
    const available = openAbove ? above : below;
    const height = Math.min(naturalHeight, available);
    panel.style.left = `${left}px`;
    panel.style.top = `${openAbove ? anchor.top - height - 8 : anchor.bottom + 8}px`;
    panel.style.maxHeight = `${available}px`;
  }
  filters.addEventListener('toggle', positionFilters);
  window.addEventListener('resize', positionFilters);
  // Capture scrolling of the roster as well as the page.
  document.addEventListener('scroll', positionFilters, true);
  positionFilters();
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
