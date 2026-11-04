(() => {
  const section = document.querySelector('[data-challenge-submissions]');
  if (!section) return;
  const form = section.querySelector('[data-submission-filters]');
  const search = form.elements.submission_q;
  const results = section.querySelector('[data-submission-results]');
  const feedback = section.querySelector('[data-submission-feedback]');
  const error = section.querySelector('[data-submission-error]');
  let timer, controller, generation = 0;
  let page = new URL(location.href).searchParams.get('submission_page') || '1';
  function invalidate() {
    clearTimeout(timer); generation++; controller?.abort();
  }
  async function load() {
    const ticket = ++generation;
    controller?.abort(); controller = new AbortController();
    const url = new URL(location.href);
    for (const [key, value] of new FormData(form)) url.searchParams.set(key, value);
    url.searchParams.set('submission_page', page);
    url.searchParams.set('submissions_fragment', '1');
    results.setAttribute('aria-busy', 'true');
    feedback.textContent = 'Loading submissions…'; error.hidden = true;
    try {
      const response = await fetch(url, {signal: controller.signal, credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok || response.headers.get('X-Submission-Fragment') !== '1') throw Error('Unable to load');
      const html = await response.text();
      if (ticket !== generation) return;
      const focusPage = results.contains(document.activeElement) ? document.activeElement.dataset.submissionPage : null;
      const x = scrollX, y = scrollY;
      document.dispatchEvent(new Event('submission-results-replacing'));
      results.innerHTML = html;
      const count = results.querySelector('[data-submission-count]');
      page = count.dataset.currentPage;
      feedback.textContent = count.textContent;
      url.searchParams.delete('submissions_fragment');
      url.searchParams.set('submission_page', page);
      history.replaceState(null, '', url);
      if (focusPage) {
        const target = results.querySelector(`[data-submission-page="${focusPage}"]`) || count;
        target.tabIndex = target.matches('a') ? 0 : -1;
        target.focus({preventScroll: true});
      }
      scrollTo(x, y);
    } catch (failure) {
      if (ticket !== generation || failure.name === 'AbortError') return;
      feedback.textContent = 'Submissions could not be updated.'; error.hidden = false;
    } finally {
      if (ticket === generation) results.setAttribute('aria-busy', 'false');
    }
  }
  search.addEventListener('input', () => {
    invalidate(); page = '1'; timer = setTimeout(load, 300);
  });
  form.addEventListener('submit', event => {event.preventDefault(); invalidate(); page = '1'; load();});
  form.addEventListener('change', event => {
    if (event.target === search) return;
    invalidate(); page = '1'; load();
  });
  section.querySelector('[data-submission-clear]').addEventListener('click', () => {
    search.value = ''; search.focus({preventScroll: true}); invalidate(); page = '1'; load();
  });
  results.addEventListener('click', event => {
    const link = event.target.closest('[data-submission-page]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); invalidate();
    page = new URL(link.href).searchParams.get('submission_page'); load();
  });
  section.querySelector('[data-submission-retry]').addEventListener('click', () => {invalidate(); load();});
})();
