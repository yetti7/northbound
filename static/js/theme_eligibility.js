(() => {
  document.querySelectorAll('form').forEach((form) => {
    const date = form.querySelector('[name="completed_on"]');
    const themes = [...form.querySelectorAll('[data-theme-start]')];
    if (!date || !themes.length) return;
    function update() {
      themes.forEach((input) => {
        const eligible = Boolean(date.value && date.value >= input.dataset.themeStart && date.value <= input.dataset.themeEnd);
        input.disabled = !eligible;
        if (!eligible && input.checked) {
          input.checked = false;
          input.dispatchEvent(new Event('change', {bubbles: true}));
        }
        input.closest('.theme-date-option').querySelector('[data-theme-reason]').textContent = eligible ? '' : (date.value ? 'Unavailable for this completion date.' : 'Choose a completion date first.');
      });
    }
    date.addEventListener('input', update);
    date.addEventListener('change', update);
    update();
  });
})();
