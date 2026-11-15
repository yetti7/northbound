/* Full-page section navigation works without JS. Warn before losing any dirty form. */
(() => {
  const root = document.querySelector('[data-submission-workspace]');
  if (!root) return;
  const forms = [...root.querySelectorAll('form')];
  const snapshot = form => JSON.stringify([...form.elements].filter(field => field.name && field.name !== 'csrfmiddlewaretoken').map(field => [field.name, field.value, Boolean(field.checked), field.disabled]));
  const initial = new Map(forms.map(form => [form, snapshot(form)]));
  let submitting = null;
  forms.forEach(form => form.addEventListener('submit', () => { submitting = form; }));
  window.addEventListener('beforeunload', event => {
    const dirty = forms.some(form => form !== submitting && (form.hasAttribute('data-unsaved-state') || snapshot(form) !== initial.get(form)));
    submitting = null;
    if (!dirty) return;
    event.preventDefault(); event.returnValue = '';
  });
})();
