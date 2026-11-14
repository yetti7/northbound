const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/theme_eligibility.js', 'utf8');
function setup({dateValue = '', checked = false, historical = false, compact = false, withDate = true} = {}) {
  const handlers = {}, reason = {textContent: ''};
  const row = {hidden: false, querySelector() {return reason;}};
  const input = {value: '1', checked, disabled: false,
    dataset: {themeStart: '2026-11-01', themeEnd: '2026-11-07', themeHistorical: historical ? 'true' : undefined},
    closest() {return row;}, dispatchEvent() {this.changes = (this.changes || 0) + 1;}};
  const date = {value: dateValue, addEventListener(name, callback) {handlers[name] = callback;}};
  const form = {querySelector(selector) {return selector === '[name="completed_on"]' && withDate ? date : null;},
    querySelectorAll(selector) {return selector === '[data-theme-start]' ? [input] : [];},
    hasAttribute() {return compact;}, addEventListener(name, callback) {handlers.formChange = callback;}};
  const document = {querySelectorAll() {return [form];}, getElementById() {return {textContent: '[1]'};}};
  vm.runInNewContext(source, {document, Event: class {}});
  return {date, input, row, handlers, reason};
}
for (const dateValue of ['2026-11-01', '2026-11-07']) {
  const state = setup({dateValue});
  assert.equal(state.row.hidden, false);
  assert.equal(state.input.disabled, false);
}
for (const dateValue of ['', '2026-10-31', '2026-11-08']) {
  const state = setup({dateValue, checked: true});
  assert.equal(state.row.hidden, true);
  assert.equal(state.input.disabled, true);
  assert.equal(state.input.checked, false);
}
const changing = setup({dateValue: '2026-11-03', checked: true});
changing.date.value = '2026-11-08'; changing.handlers.input();
assert.equal(changing.row.hidden, true); assert.equal(changing.input.checked, false);
changing.date.value = '2026-11-07'; changing.handlers.change();
assert.equal(changing.row.hidden, false); assert.equal(changing.input.disabled, false);
assert.equal(changing.input.checked, false);
for (const compact of [false, true]) {
  const state = setup({dateValue: '2026-11-08', checked: true, historical: true, compact});
  assert.equal(state.row.hidden, false); assert.equal(state.input.checked, true);
  assert.equal(state.input.disabled, false); assert.match(state.reason.textContent, /Existing claim/);
  state.input.checked = false; state.handlers.change();
  assert.equal(state.row.hidden, false); assert.equal(state.input.disabled, true);
}
const focused = setup({withDate: false});
assert.equal(focused.row.hidden, false); // Server-rendered state is retained when the date is fixed.
console.log('Theme visibility checks passed: inclusive boundaries, missing dates, date changes, selection clearing, historical claims, and fixed-date forms.');
