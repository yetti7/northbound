/* Reader intent only. Identity matching and qualification remain server-side. */
(() => {
  const picker = document.querySelector('[data-challenge-book-picker]');
  const form = document.querySelector('[data-submission-form]');
  if (!picker || !form) return;
  const feedback = picker.querySelector('[data-selection-feedback]');
  let timer, sequence = 0;
  const dismissed = new Set();
  const input = (name) => form.elements.namedItem(name);
  const groups = [...picker.querySelectorAll('[data-selection-group]')];
  async function preview() {
    const requestSequence = ++sequence;
    if (!input('title').value.trim()) { feedback.replaceChildren(); return; }
    const body = new FormData(form);
    body.set('action', 'intent-preview');
    try {
      const response = await fetch(picker.dataset.previewUrl, {method: 'POST', body, credentials: 'same-origin'});
      const result = await response.json();
      if (requestSequence !== sequence) return;
      feedback.replaceChildren();
      if (!result.ok) { feedback.textContent = result.message; return; }
      result.feedback.forEach(item => {
        const section = document.createElement('section');
        const heading = document.createElement('h3'); heading.textContent = item.domain === 'botm' ? 'Book of the Month' : 'Personal TBR';
        const message = document.createElement('p'); message.textContent = item.message;
        section.append(heading, message);
        item.suggestions.forEach(suggestion => {
          const key = `${item.domain}:${suggestion.title}:${suggestion.author}:${input('title').value}:${input('author').value}`;
          if (dismissed.has(key)) return;
          const box = document.createElement('div');
          const title = document.createElement('strong'); title.textContent = `Possible ${heading.textContent} Match: ${suggestion.title} — ${suggestion.author}`;
          const reason = document.createElement('p'); reason.textContent = suggestion.message;
          const request = document.createElement('button'); request.type = 'button'; request.textContent = 'Request Review'; request.className = 'button secondary';
          request.addEventListener('click', () => {
            const choice = input(`${item.domain}_choice`);
            const option = [...choice.options].find(option => option.dataset.entry === String(suggestion.entry));
            if (!option || option.disabled) return;
            choice.value = option.value;
            choice.dispatchEvent(new Event('change', {bubbles:true}));
          });
          const dismiss = document.createElement('button'); dismiss.type = 'button'; dismiss.textContent = 'Dismiss'; dismiss.className = 'button secondary';
          dismiss.addEventListener('click', () => { dismissed.add(key); box.remove(); });
          box.append(title, reason, request, dismiss); section.append(box);
        });
        feedback.append(section);
      });
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
        if (label === 'Remove') { input(`selected_${domain}`).value = ''; input(`${domain}_token`).value = ''; input(`${domain}_choice`).value = ''; render(group); schedulePreview(); }
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
      input(`${domain}_choice`).value = card.dataset.token;
      // Never replace chosen edition metadata or overwrite entered book information.
      if (!input('catalog_selection').value && ((!input('title').value.trim() && !input('author').value.trim()) || replacePrefill)) {
        input('title').value = card.dataset.title; input('author').value = card.dataset.author;
      }
      render(group); group.querySelector('details').open = false;
      group.querySelector('[data-selection-summary] button').focus(); schedulePreview();
    }));
    const choice = input(`${group.dataset.selectionGroup}_choice`);
    choice.addEventListener('change', () => {
      const domain = group.dataset.selectionGroup;
      input(`selected_${domain}`).value = choice.selectedOptions[0]?.dataset.entry || '';
      input(`${domain}_token`).value = choice.value;
      render(group); schedulePreview();
    });
    input(`selected_${group.dataset.selectionGroup}`).value = choice.selectedOptions[0]?.dataset.entry || '';
    input(`${group.dataset.selectionGroup}_token`).value = choice.value;
    render(group);
  });
  // Existing pending associations are retained unless explicitly removed.
  document.querySelector('[data-clear-book]')?.addEventListener('click', () => {
    const clearAssociation = input('clear_catalog');
    if (clearAssociation) clearAssociation.checked = true;
  });
  document.addEventListener('northbound-pages-selected', () => {
    const clearAssociation = input('clear_catalog');
    if (clearAssociation) clearAssociation.checked = false;
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
