(() => {
  const form = document.querySelector('[data-unified-review]');
  if (!form) return;
  function update() {
    const status = form.querySelector('[name="status"]');
    const domains = ['botm','tbr'].filter(domain => form.querySelector(`[name="confirm_${domain}"]`)?.checked);
    form.querySelector('[data-review-submit]').textContent = status.value === 'approved'
      ? 'Approve Book' + (domains.length ? ' + Confirm ' + domains.map(domain => domain.toUpperCase()).join(' & ') : '')
      : 'Save Review';
  }
  form.addEventListener('change', update); update();
})();
