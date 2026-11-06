/* Reader intent only. Identity matching and qualification remain server-side. */
(() => {
  const picker = document.querySelector('[data-challenge-book-picker]');
  const form = document.querySelector('[data-submission-form]');
  if (!picker || !form) return;
  const feedback = picker.querySelector('[data-selection-feedback]');
  let timer, sequence = 0;
  const input = (name) => form.elements.namedItem(name);
  const groups = [...picker.querySelectorAll('[data-selection-group]')];
  async function preview() {
    const requestSequence = ++sequence;
    if (!input('selected_botm').value && !input('selected_tbr').value) { feedback.textContent = ''; return; }
    const body = new FormData(form);
    body.set('action', 'intent-preview');
    try {
      const response = await fetch(picker.dataset.previewUrl, {method: 'POST', body, credentials: 'same-origin'});
      const result = await response.json();
      if (requestSequence !== sequence) return;
      feedback.textContent = result.ok
        ? result.feedback.map(item => `${item.domain === 'botm' ? 'BOTM' : 'Personal TBR'}: ${item.message}`).join(' ')
        : result.message;
    } catch (_) {
      if (requestSequence === sequence) feedback.textContent = 'Your selection is kept. Qualification will be checked when you submit.';
    }
  }
  function schedulePreview() { ++sequence; clearTimeout(timer); timer = setTimeout(preview, 250); }
  function render(group) {
    const domain = group.dataset.selectionGroup;
    const options = group.querySelector('[data-selection-options]');
    const summary = group.querySelector('[data-selection-summary]');
    const selected = [...group.querySelectorAll('[data-challenge-book]')].find(card => card.dataset.challengeBook === input(`selected_${domain}`).value);
    group.querySelectorAll('[data-challenge-book]').forEach(card => card.setAttribute('aria-pressed', String(card === selected)));
    summary.replaceChildren(); summary.hidden = !selected;
    if (!selected) return;
    const text = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = `${domain === 'botm' ? 'BOTM' : 'Personal TBR'} selected: ${selected.dataset.title}`;
    const bonus = document.createElement('p'); bonus.textContent = selected.dataset.bonusSummary;
    text.append(title, bonus); summary.append(text);
    for (const label of ['Change', 'Remove']) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'button secondary'; button.textContent = label;
      button.setAttribute('aria-label', `${label} ${domain === 'botm' ? 'BOTM' : 'Personal TBR'} selection`);
      button.addEventListener('click', () => {
        if (label === 'Remove') { input(`selected_${domain}`).value = ''; input(`${domain}_token`).value = ''; render(group); schedulePreview(); }
        options.open = true; options.querySelector('summary').focus();
      });
      summary.append(button);
    }
  }
  groups.forEach(group => {
    group.querySelectorAll('[data-challenge-book]').forEach(card => card.addEventListener('click', () => {
      const domain = card.dataset.domain;
      const previous = [...group.querySelectorAll('[data-challenge-book]')].find(item => item.dataset.challengeBook === input(`selected_${domain}`).value);
      const replacePrefill = previous && input('title').value === previous.dataset.title && input('author').value === previous.dataset.author;
      input(`selected_${domain}`).value = card.dataset.challengeBook;
      input(`${domain}_token`).value = card.dataset.token;
      // Never replace chosen edition metadata or overwrite entered book information.
      if (!input('catalog_selection').value && ((!input('title').value.trim() && !input('author').value.trim()) || replacePrefill)) {
        input('title').value = card.dataset.title; input('author').value = card.dataset.author;
      }
      render(group); group.querySelector('details').open = false;
      group.querySelector('[data-selection-summary] button').focus(); schedulePreview();
    }));
    render(group);
  });
  form.addEventListener('input', schedulePreview);
  document.addEventListener('northbound-pages-selected', schedulePreview);
  // Manual entry must also work when Hardcover is disconnected.
  if (!document.querySelector('[data-catalog-tools]')) {
    document.querySelector('[data-clear-book]')?.addEventListener('click', () => {
      ['title', 'author', 'submitted_pages', 'reference_url', 'catalog_selection'].forEach(name => { input(name).value = ''; });
      input('submitted_pages').readOnly = false; input('title').focus(); schedulePreview();
    });
  }
  schedulePreview();
})();
