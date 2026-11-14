(() => {
  const panel = document.querySelector('[data-edit-association]');
  const form = document.querySelector('[data-correction-form]');
  if (!form) return;
  let generation = 0, controller;
  const clearPreview = () => {
    document.querySelector('[data-correction-preview]')?.setAttribute('hidden', '');
    form.elements.preview_token.value = '';
    if (form.elements.confirm_approved) form.elements.confirm_approved.checked = false;
    if (form.elements.confirm_work) form.elements.confirm_work.checked = false;
    form.querySelector('[data-correction-submit]').textContent = form.hasAttribute('data-compact-editor') ? 'Save' : 'Preview Changes';
  };
  form.addEventListener('input', event => {if (!['confirm_approved', 'confirm_work'].includes(event.target.name)) clearPreview();});
  form.addEventListener('change', event => {if (!['confirm_approved', 'confirm_work'].includes(event.target.name)) clearPreview();});
  if (!panel) return;
  function updateAssociationControls() {
    const changed = Boolean(form.elements.edition.value && form.elements.edition.value !== panel.dataset.currentEdition);
    const options = form.querySelector('[data-association-options]');
    const override = form.querySelector('[data-manual-override]');
    if (options) options.hidden = !changed;
    if (override) override.hidden = !changed;
  }
  const results = panel.querySelector('[data-association-results]');
  const status = panel.querySelector('[data-association-status]');
  const selection = panel.querySelector('[data-association-selection]');
  async function request(action, values) {
    const ticket = ++generation;
    controller?.abort(); controller = new AbortController();
    status.textContent = 'Loading Hardcover…';
    const data = new FormData(); data.set('action', action);
    Object.entries(values).forEach(([key, value]) => data.set(key, value));
    try {
      const response = await fetch(panel.dataset.url, {method: 'POST', body: data,
        headers: {'X-CSRFToken': form.elements.csrfmiddlewaretoken.value}, signal: controller.signal});
      const body = await response.json();
      if (ticket !== generation) return null;
      if (!response.ok || !body.ok) throw Error(body.message || 'Hardcover lookup failed.');
      status.textContent = ''; return body;
    } catch (error) {
      if (ticket === generation && error.name !== 'AbortError') status.textContent = error.message;
      return null;
    }
  }
  function option(label, handler) {
    const row = document.createElement('p'); const button = document.createElement('button');
    button.type = 'button'; button.className = 'button secondary'; button.textContent = label;
    button.addEventListener('click', handler); row.append(button); results.append(row);
  }
  async function search() {
    const body = await request('search', {query: panel.querySelector('input').value});
    if (!body) return; results.replaceChildren();
    if (!body.results.length) status.textContent = 'No matching Hardcover works found.';
    body.results.forEach(work => option(`${work.title} — ${work.author || ''}`, async () => {
      const editions = await request('editions', {book_id: work.book_id});
      if (!editions) return; results.replaceChildren();
      if (!editions.editions.length) status.textContent = 'No editions available for this work.';
      editions.editions.forEach(edition => option(`${edition.format || 'Edition'} · Hardcover #${edition.edition_id} · ${edition.pages || 'No'} pages`, async () => {
        const chosen = await request('edition', {book_id: work.book_id, edition_id: edition.edition_id});
        if (!chosen) return;
        form.elements.edition.value = chosen.edition_pk; clearPreview(); results.replaceChildren();
        updateAssociationControls();
        form.elements.association_mode.focus({preventScroll: true});
        const book = chosen.selected, scoring = chosen.scoring;
        const duration = book.audio_seconds ? ` Listening duration: ${(book.audio_seconds / 3600).toFixed(1)} hours (reference only).` : '';
        selection.textContent = `Selected ${book.format || 'edition'}: ${book.title || work.title}, Hardcover #${book.edition_id}.${duration} ` +
          (scoring ? `Scoring: ${scoring.format}, Hardcover #${scoring.edition_id}, ${scoring.pages} pages. Preview the full correction before confirming.` :
          'No usable ebook/print equivalent found. Associate Only can preserve existing pages, or use an explicit manual page override requiring manual verification.');
      }));
    }));
  }
  panel.querySelector('[data-association-keep]').addEventListener('click', () => {
    ++generation; controller?.abort(); form.elements.edition.value = panel.dataset.currentEdition;
    if (form.hasAttribute('data-compact-editor')) {
      form.elements.association_mode.value = 'update';
      form.elements.manual_page_override.checked = false;
    }
    updateAssociationControls();
    clearPreview(); results.replaceChildren(); selection.textContent = 'Current association retained.'; status.textContent = '';
  });
  panel.querySelector('[data-association-search]').addEventListener('click', search);
  panel.querySelector('input').addEventListener('keydown', event => {if (event.key === 'Enter') {event.preventDefault(); search();}});
})();
