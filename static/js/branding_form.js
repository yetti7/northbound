(() => {
  const form = document.querySelector('[data-branding-form]');
  if (!form) return;
  if (form.hasAttribute('data-group-branding-form')) {
    const header = document.querySelector('[data-group-identity-preview]');
    const mode = form.elements.namedItem('branding_mode');
    const assets = {};
    function display(image, url) {
      if (url) image.src = url;
      else image.removeAttribute('src');
      image.hidden = !url;
    }
    function render() {
      const banner = mode.value === 'banner' ? assets.banner.current : '';
      display(header.querySelector('[data-group-preview-logo]'), assets.logo.current);
      display(header.querySelector('[data-group-preview-banner]'), banner);
      header.classList.toggle('has-banner', Boolean(banner));
      header.classList.toggle('has-logo', Boolean(assets.logo.current));
    }
    ['logo', 'banner'].forEach(kind => {
      const input = form.elements.namedItem(kind);
      const clear = form.elements.namedItem(kind + '-clear');
      const preview = document.querySelector(`[data-branding-preview="${kind}"]`);
      assets[kind] = {stored: preview.getAttribute('src') || '', current: '', objectUrl: ''};
      function refresh() {
        const asset = assets[kind];
        if (asset.objectUrl) URL.revokeObjectURL(asset.objectUrl);
        asset.objectUrl = '';
        const file = input.files[0];
        if (file && ['image/png', 'image/jpeg', 'image/webp'].includes(file.type) && !clear?.checked) {
          asset.objectUrl = URL.createObjectURL(file);
        }
        asset.current = clear?.checked ? '' : asset.objectUrl || asset.stored;
        display(preview, asset.current);
      }
      input.addEventListener('change', () => {
        if (input.files.length && clear) clear.checked = false;
        refresh(); render();
      });
      if (clear) clear.addEventListener('change', () => {
        if (clear.checked) input.value = '';
        refresh(); render();
      });
      refresh();
    });
    mode.addEventListener('change', render);
    render();
    return;
  }
  ['logo','banner'].forEach(kind => {
    const input = form.querySelector(`[name="${kind}"]`);
    const preview = document.querySelector(`[data-branding-preview="${kind}"]`);
    const source = document.querySelector(`[data-branding-source="${kind}"]`);
    const clear = form.querySelector(`[name="${kind}-clear"]`);
    const original = preview.getAttribute('src') || '';
    let objectUrl;
    function refresh() {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = '';
      const file = input.files[0];
      if (file && ['image/png','image/jpeg','image/webp'].includes(file.type) && !clear?.checked) {
        objectUrl = URL.createObjectURL(file);
      }
      const removed = Boolean(clear?.checked);
      const url = removed ? preview.dataset.fallback || '' : objectUrl || original;
      if (url) preview.src = url;
      else preview.removeAttribute('src');
      preview.hidden = !url;
      if (source) source.textContent = removed ? preview.dataset.fallbackLabel : objectUrl ? 'Selected replacement — not saved' : preview.dataset.sourceLabel;
    }
    input.addEventListener('change', () => {
      if (input.files.length && clear) clear.checked = false;
      refresh();
    });
    if (clear) clear.addEventListener('change', () => {
      if (clear.checked) input.value = '';
      refresh();
    });
    refresh();
  });
})();
