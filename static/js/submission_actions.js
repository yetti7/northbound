/* Portal menus outside scrolling tables so sticky cells cannot clip them. */
(() => {
  let active = null;
  const items = menu => [...menu.querySelectorAll('[role="menuitem"]')];
  function close(restore = false) {
    if (!active) return;
    const {button, menu, home} = active;
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    home.append(menu);
    active = null;
    if (restore) button.focus();
  }
  function position() {
    if (!active) return;
    const {button, menu} = active;
    const rect = button.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth) {
      close(); return;
    }
    menu.style.maxHeight = `${Math.max(0, innerHeight - 16)}px`;
    const box = menu.getBoundingClientRect();
    const below = innerHeight - rect.bottom - 8;
    const above = rect.top - 8;
    const upward = box.height > below && above > below;
    menu.style.maxHeight = `${Math.max(0, upward ? above : below)}px`;
    const height = menu.getBoundingClientRect().height;
    menu.style.left = `${Math.max(8, Math.min(rect.right - box.width, innerWidth - box.width - 8))}px`;
    menu.style.top = `${upward ? rect.top - height - 4 : rect.bottom + 4}px`;
  }
  function open(button, last = false) {
    close();
    const home = button.closest('[data-submission-actions]');
    const menu = home.querySelector('[role="menu"]');
    active = {button, menu, home};
    document.body.append(menu);
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    position();
    (last ? items(menu).at(-1) : items(menu)[0])?.focus();
  }
  document.querySelectorAll('[data-submission-actions] button').forEach(button => {
    button.addEventListener('click', () => active?.button === button ? close(true) : open(button));
    button.addEventListener('keydown', event => {
      if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault(); event.stopPropagation(); open(button, event.key === 'ArrowUp');
      }
    });
  });
  document.addEventListener('pointerdown', event => {
    if (active && !active.menu.contains(event.target) && !active.button.contains(event.target)) close();
  });
  document.addEventListener('focusin', event => {
    if (active && !active.menu.contains(event.target) && event.target !== active.button) close();
  });
  document.addEventListener('click', event => {
    if (active && active.menu.contains(event.target)) close();
  });
  document.addEventListener('keydown', event => {
    if (!active) return;
    if (event.key === 'Escape') { event.preventDefault(); close(true); return; }
    if (event.key === 'Tab') { close(true); return; }
    const links = items(active.menu);
    let index = links.indexOf(document.activeElement);
    if (event.key === 'ArrowDown') index = (index + 1) % links.length;
    else if (event.key === 'ArrowUp') index = (index - 1 + links.length) % links.length;
    else if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = links.length - 1;
    else return;
    event.preventDefault(); links[index].focus();
  });
  window.addEventListener('resize', position);
  document.addEventListener('scroll', position, true);
})();
