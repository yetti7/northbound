/* Estimated only: staff must approve the book and the separate Immersive request. */
(() => {
  const preview = document.querySelector('[data-immersive-preview]');
  if (!preview) return;
  const form = preview.closest('form'), pages = form.elements.submitted_pages, request = form.elements.immersive_request;
  function update() {
    const base = Number(pages.value), percent = Number(preview.dataset.percent);
    if (!Number.isSafeInteger(base) || base <= 0 || base > 2147483647) {preview.textContent = 'Enter pages to preview potential scoring.'; return;}
    // Positive integer arithmetic implements the server's single round-half-up.
    const bonus = request?.checked ? Math.floor((base * percent + 50) / 100) : 0;
    const format = value => value.toLocaleString();
    preview.textContent = `Estimated Base Pages: ${format(base)} · Immersive Reading (${percent}%): +${format(bonus)} · Potential Total: ${format(base + bonus)}. ` +
      'Estimate excludes other bonuses. Book pages and Immersive Reading require their applicable approvals.';
  }
  form.addEventListener('input', update); form.addEventListener('change', update);
  document.addEventListener('northbound-pages-selected', update); update();
})();
