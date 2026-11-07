const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/submission_association.js', 'utf8');

function setup(withCatalog) {
  const events = {}, buttons = {}, preview = {hidden: false, setAttribute() {this.hidden = true;}};
  const submit = {textContent: 'Confirm Previewed Changes'};
  const form = {
    elements: {preview_token: {value: 'signed'}, confirm_approved: {checked: true}},
    addEventListener(name, callback) {events[name] = callback;},
    querySelector() {return submit;},
  };
  const panel = withCatalog ? {
    dataset: {currentEdition: '17'},
    querySelector(selector) {
      if (!buttons[selector]) buttons[selector] = {
        addEventListener(name, callback) {this[name] = callback;}, replaceChildren() {}, textContent: '',
      };
      return buttons[selector];
    },
  } : null;
  if (withCatalog) Object.assign(form.elements, {
    edition: {value: '21'}, association_mode: {value: 'only'}, confirm_work: {checked: true},
  });
  const document = {querySelector(selector) {
    if (selector === '[data-edit-association]') return panel;
    if (selector === '[data-correction-form]') return form;
    if (selector === '[data-correction-preview]') return preview;
    throw new Error('Unexpected selector: ' + selector);
  }};
  vm.runInNewContext(source, {document});
  return {form, events, buttons, preview, submit};
}

const pages = setup(false);
pages.events.input({target: {name: 'confirm_approved'}});
assert.equal(pages.form.elements.preview_token.value, 'signed');
pages.events.change({target: {name: 'submitted_pages'}});
assert.equal(pages.form.elements.preview_token.value, '');
assert.equal(pages.form.elements.confirm_approved.checked, false);
assert.equal(pages.preview.hidden, true);
assert.equal(pages.submit.textContent, 'Preview Changes');

const book = setup(true);
book.buttons['[data-association-keep]'].click();
assert.equal(book.form.elements.edition.value, '17');
assert.equal(book.form.elements.association_mode.value, 'only');
assert.equal(book.form.elements.preview_token.value, '');
assert.equal(book.form.elements.confirm_work.checked, false);
console.log('PASS: pages invalidate previews without catalog controls; keeping association preserves mode and clears confirmation.');
