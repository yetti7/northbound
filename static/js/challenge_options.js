/* Native disclosure provides click/tap/Enter/Space and ordinary link tab order. */
(() => {
  document.querySelectorAll('[data-challenge-options]').forEach((menu) => {
    const trigger = menu.querySelector('summary');
    const links = [...menu.querySelectorAll('a[href]')];
    const update = () => trigger.setAttribute('aria-expanded', String(menu.open));
    const close = () => { menu.open = false; update(); };
    menu.addEventListener('toggle', update);
    update();
    document.addEventListener('pointerdown', (event) => {
      if (menu.open && !menu.contains(event.target)) close();
    }, true);
    document.addEventListener('keydown', (event) => {
      if ((event.key === 'Escape' || event.key === 'Esc') && menu.open) {
        event.preventDefault(); close(); trigger.focus();
      }
    });
    menu.addEventListener('keydown', (event) => {
      if (!['ArrowDown', 'ArrowUp'].includes(event.key) || !links.length) return;
      event.preventDefault(); menu.open = true; update();
      const current = links.indexOf(document.activeElement);
      const next = current < 0 ? (event.key === 'ArrowDown' ? 0 : links.length - 1)
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length;
      links[next].focus();
    });
  });
})();
