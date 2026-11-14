document.querySelectorAll('[data-community-image]').forEach(image => {
  image.addEventListener('error', () => { image.hidden = true; });
  if (image.complete && image.getAttribute('src') && !image.naturalWidth) image.hidden = true;
});
