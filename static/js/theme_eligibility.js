(() => {
  document.querySelectorAll('form').forEach((form) => {
    const date = form.querySelector('[name="completed_on"]');
    const themes = [...form.querySelectorAll('[data-theme-start]')];
    if (!date || !themes.length) return;
    const compact = form.hasAttribute('data-compact-editor');
    const original = compact
      ? JSON.parse(document.getElementById('submission-original-themes').textContent).map(String)
      : [];
    function updateResponses() {
      const selected = new Set(themes.filter(input => input.checked).map(input => input.value));
      form.querySelectorAll('[data-theme-response]').forEach(field => {
        field.hidden = !selected.has(field.dataset.themeResponse) && !field.querySelector('.errorlist');
      });
      const removal = form.querySelector('[data-theme-removal]');
      if (removal) removal.hidden = original.every(id => selected.has(id)) && !removal.querySelector('.errorlist');
    }
    function update() {
      themes.forEach((input) => {
        const eligible = Boolean(date.value && date.value >= input.dataset.themeStart && date.value <= input.dataset.themeEnd);
        const historical = input.dataset.themeHistorical === 'true';
        input.closest('.theme-date-option').hidden = !eligible && !historical;
        input.disabled = !eligible && !(historical && input.checked);
        if (!historical && !eligible && input.checked) {
          input.checked = false;
          input.dispatchEvent(new Event('change', {bubbles: true}));
        }
        input.closest('.theme-date-option').querySelector('[data-theme-reason]').textContent = eligible ? '' : (historical ? 'Existing claim: unavailable for this completion date. Change the date or explicitly remove the claim before saving.' : (date.value ? 'Unavailable for this completion date.' : 'Choose a completion date first.'));
      });
      if (compact) updateResponses();
    }
    if (compact) form.addEventListener('change', event => { if (event.target.name === 'themes') update(); });
    date.addEventListener('input', update);
    date.addEventListener('change', update);
    update();
  });
})();
