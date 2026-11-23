(() => {
  const panel = document.querySelector('[data-banner-appearance]');
  if (!panel) return;
  const enabled = panel.querySelector('[name="gradient_enabled"]');
  const sliders = ['gradient_coverage', 'gradient_darkness'].map(name => panel.querySelector(`[name="${name}"]`));
  function update() {
    sliders.forEach(input => {
      panel.querySelector(`[data-appearance-value="${input.name}"]`).textContent = `${input.value}%`;
    });
    document.querySelectorAll?.('.group-branding-preview header').forEach(header => {
      header.classList.toggle('branding-custom-gradient', enabled.checked);
      header.style.setProperty('--branding-coverage', `${sliders[0].value}%`);
      header.style.setProperty('--branding-darkness', String(Number(sliders[1].value) / 100));
    });
    panel.querySelector('[data-appearance-status]').textContent = enabled.checked
      ? 'Customized gradient enabled.'
      : 'Legacy appearance active. Slider values apply only when enabled.';
  }
  sliders.forEach(input => input.addEventListener('input', update));
  enabled.addEventListener('change', update);
  panel.querySelector('[data-appearance-defaults]')?.addEventListener('click', () => {
    enabled.checked = false;
    sliders.forEach(input => { input.value = input.dataset.default; });
    update();
  });
  update();
})();
