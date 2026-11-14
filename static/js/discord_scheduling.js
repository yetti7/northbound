document.querySelectorAll('[data-discord-report]').forEach(panel => {
  const form = panel.querySelector('[data-report-form]');
  if (!form) return;
  const enabled = form.elements.namedItem('scheduled_enabled');
  const frequency = form.elements.namedItem('frequency');
  const controls = panel.querySelector('[data-frequency-controls]');
  const times = panel.querySelector('[data-fixed-times]');
  const days = panel.querySelector('[data-weekdays]');
  function display(section, visible) {
    section.hidden = !visible;
    section.querySelectorAll('input,select,textarea').forEach(input => { input.disabled = !visible; });
  }
  function update() {
    display(controls, enabled.checked);
    display(times, enabled.checked && ['daily','weekly','custom'].includes(frequency.value));
    display(days, enabled.checked && ['weekly','custom'].includes(frequency.value));
  }
  enabled.addEventListener('change', () => {
    if (enabled.checked && frequency.value === 'disabled') frequency.value = '1h';
    update();
  });
  frequency.addEventListener('change', () => {
    if (frequency.value === 'disabled') enabled.checked = false;
    update();
  });
  update();
});
