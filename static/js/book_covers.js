(function (root) {
  function safeUrl(value) {
    try {
      const url = new URL(value);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_) { return ''; }
  }
  function update(container, value, title) {
    const image = container.querySelector('[data-book-cover-image], [data-tbr-cover-image]');
    const placeholder = container.querySelector('[data-book-cover-placeholder], [data-tbr-cover-placeholder]');
    if (!image || !placeholder) return;
    const url = safeUrl(value);
    placeholder.textContent = ((title || '').trim()[0] || '?').toUpperCase();
    image.alt = `Cover of ${title || 'selected book'}`;
    const failed = () => { image.hidden = true; placeholder.hidden = false; };
    const loaded = () => { image.hidden = false; placeholder.hidden = true; };
    image.onerror = failed;
    image.onload = loaded;
    if (image.dataset.coverUrl !== url) {
      image.dataset.coverUrl = url;
      image.hidden = !url;
      placeholder.hidden = Boolean(url);
      if (url) image.setAttribute('src', url);
      else image.removeAttribute('src');
    }
    if (url && image.complete) {
      if (image.naturalWidth > 0) loaded();
      else failed();
    }
  }
  function initialize() {
    document.querySelectorAll('[data-book-cover]').forEach((container) => {
      const image = container.querySelector('[data-book-cover-image]');
      update(container, image.getAttribute('src') || '', image.alt.replace(/^Cover of /, ''));
    });
  }
  root.NorthboundBookCovers = {safeUrl, update};
  if (typeof module !== 'undefined') module.exports = root.NorthboundBookCovers;
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
    else initialize();
  }
})(typeof window !== 'undefined' ? window : globalThis);
