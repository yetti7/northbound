(() => {
  document.querySelectorAll('[data-wrapped]').forEach(root => {
    const slides = [...root.querySelectorAll('[data-wrapped-slide]')];
    if (!slides.length) return;
    const stage = root.querySelector('[data-wrapped-stage]');
    const previous = root.querySelector('[data-wrapped-prev]');
    const next = root.querySelector('[data-wrapped-next]');
    const status = root.querySelector('[data-wrapped-status]');
    const progress = root.querySelector('[data-wrapped-progress]');
    let index = 0;
    let pointer = null;
    function show(requested, focus = true) {
      index = Math.max(0, Math.min(slides.length - 1, requested));
      slides.forEach((slide, position) => { slide.hidden = position !== index; });
      previous.disabled = index === 0;
      next.disabled = index === slides.length - 1;
      status.textContent = `Slide ${index + 1} of ${slides.length}`;
      progress.value = index + 1;
      stage.scrollTop = 0;
      slides[index].querySelectorAll('[data-cover-src]').forEach(image => {
        const cover = image.closest('[data-book-cover]');
        const helper = window.NorthboundBookCovers;
        if (helper && cover) {
          helper.update(cover, image.dataset.coverSrc, image.alt.replace(/^Cover of /, ''));
          image.removeAttribute('data-cover-src');
        }
      });
      if (focus) slides[index].focus({preventScroll: true});
    }
    root.setAttribute('data-wrapped-enhanced', '');
    root.querySelector('[data-wrapped-controls]').hidden = false;
    previous.addEventListener('click', () => show(index - 1));
    next.addEventListener('click', () => show(index + 1));
    root.addEventListener('keydown', event => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.defaultPrevented ||
          event.target.closest('input, select, textarea, a, [contenteditable="true"]')) return;
      const destination = {ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: slides.length - 1}[event.key];
      if (destination === undefined) return;
      event.preventDefault();
      show(destination);
    });
    stage.addEventListener('pointerdown', event => {
      if (event.pointerType === 'mouse' || !event.isPrimary) return;
      pointer = {id: event.pointerId, x: event.clientX, y: event.clientY, at: event.timeStamp};
    });
    stage.addEventListener('pointercancel', () => { pointer = null; });
    stage.addEventListener('pointerup', event => {
      if (!pointer || pointer.id !== event.pointerId) return;
      const dx = event.clientX - pointer.x;
      const dy = event.clientY - pointer.y;
      const elapsed = event.timeStamp - pointer.at;
      pointer = null;
      if (elapsed < 1000 && Math.abs(dx) >= 50 && Math.abs(dy) < Math.abs(dx) * 0.6) {
        show(index + (dx < 0 ? 1 : -1));
      }
    });
    show(0, false);
  });
})();
