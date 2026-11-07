const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(dataset = {}) { this.dataset = dataset; this.children = []; this.events = {}; this.hidden = false; this.value = ''; this.attrs = {}; }
  addEventListener(name, cb) {this.events[name] = cb;}
  setAttribute(key,value) {this.attrs[key] = value;}
  append(...items) {this.children.push(...items);}
  replaceChildren() {this.children = [];}
  focus() {this.focused = true;}
  querySelector(selector) {
    if (selector === 'summary') return this.heading;
    if (selector === '[data-selection-options]' || selector === 'details') return this.options;
    if (selector === '[data-selection-summary]') return this.summary;
    if (selector === '[data-selection-summary] button') return this.summary.children.find(x => x.type === 'button');
    throw Error(selector);
  }
  querySelectorAll() {return this.cards;}
}
const inputs = {};
for (const name of ['selected_botm','selected_tbr','botm_token','tbr_token','catalog_selection','title','author','submitted_pages','reference_url']) inputs[name] = new Element();
const form = new Element(); form.elements = {namedItem:name => inputs[name]};
function group(domain) {
  const element = new Element({selectionGroup:domain});
  element.summary = new Element(); element.options = new Element(); element.options.heading = new Element();
  element.cards = [1,2].map(id => new Element({domain,challengeBook:String(id),token:`${domain}-${id}`,title:`Book ${id}`,author:'Author',bonusSummary:'Potential credit after approval.'}));
  return element;
}
const groups = [group('botm'),group('tbr')];
const picker = new Element(); picker.querySelectorAll = () => groups;
const feedback = new Element(); picker.querySelector = () => feedback;
const document = new Element();
document.querySelector = selector => ({'[data-challenge-book-picker]':picker,'[data-submission-form]':form,'[data-catalog-tools]':{}}[selector]);
document.createElement = () => new Element();
vm.runInNewContext(fs.readFileSync(__dirname+'/challenge_book_picker.js','utf8'), {document, setTimeout:()=>0, clearTimeout(){}});
groups[0].cards[0].events.click();
assert.equal(inputs.selected_botm.value,'1'); assert.equal(inputs.botm_token.value,'botm-1');
assert.equal(inputs.title.value,'Book 1');assert.equal(groups[0].cards[0].attrs['aria-pressed'],'true');
groups[1].cards[0].events.click();
assert.equal(inputs.selected_botm.value,'1');assert.equal(inputs.selected_tbr.value,'1');
// A new manual selection replaces only the untouched prior prefill.
groups[0].cards[1].events.click();assert.equal(inputs.title.value,'Book 2');
// Simulate Hardcover applying an alternate edition, then running its existing event.
inputs.catalog_selection.value='signed-edition';inputs.title.value='German title';
document.events['northbound-pages-selected']();
assert.equal(inputs.selected_botm.value,'2');assert.equal(inputs.selected_tbr.value,'1');
groups[0].cards[0].events.click();assert.equal(inputs.title.value,'German title');assert.equal(inputs.catalog_selection.value,'signed-edition');
const remove = groups[0].summary.children.find(item=>item.textContent==='Remove');remove.events.click();
assert.equal(inputs.selected_botm.value,'');assert.equal(inputs.selected_tbr.value,'1');assert.equal(inputs.catalog_selection.value,'signed-edition');
console.log('Challenge picker interaction checks passed.');
