(() => {
  const form = document.querySelector('[data-recap-filters]');
  if (!form) return;
  function clear(name) {const field = form.elements.namedItem(name); if (field) field.value = '';}
  form.elements.namedItem('group').addEventListener('change', () => {
    clear('challenge'); clear('reader'); clear('team'); form.requestSubmit();
  });
  form.elements.namedItem('perspective').addEventListener('change', () => {
    clear('reader'); clear('team'); form.requestSubmit();
  });
  form.elements.namedItem('challenge').addEventListener('change', () => {
    clear('reader'); clear('team'); form.requestSubmit();
  });
})();
