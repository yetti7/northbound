/* Focused browser-independent event checks for the optional picker enhancement. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.children = []; this.listeners = {}; this.dataset = {}; this.hidden = false; this.value = ''; }
  append(...children) { children.forEach(child => { child.parent = this; this.children.push(child); }); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  dispatchEvent(event) { this.listeners[event.type]?.(event); }
  click() { this.listeners.click?.(); }
  focus() {}
  setAttribute(name, value) { this[name] = value; }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
}
(async () => {
  const feedback = new Element(), summary = new Element(), details = new Element();
  details.querySelector = () => new Element();
  const card = new Element();
  card.dataset = {challengeBook:'7', domain:'tbr', token:'signed', title:'Death Valley', author:'Karina Halle', bonusSummary:'After review'};
  const group = new Element(); group.dataset.selectionGroup = 'tbr';
  group.querySelectorAll = () => [card];
  group.querySelector = selector => selector === '[data-selection-options]' ? details : summary;
  const controls = {};
  ['title','author','catalog_selection','selected_tbr','tbr_token','selected_botm','botm_token'].forEach(name => controls[name] = new Element());
  controls.title.value = 'Death Valley'; controls.author.value = 'Karin Hale';
  const choice = controls.tbr_choice = new Element();
  choice.options = [{value:'',dataset:{}}, {value:'signed',dataset:{entry:'7'}}];
  Object.defineProperty(choice, 'selectedOptions', {get: () => choice.options.filter(option => option.value === choice.value)});
  const form = new Element(); form.elements = {namedItem: name => controls[name]};
  const picker = new Element(); picker.dataset.previewUrl = '/preview';
  picker.querySelector = () => feedback; picker.querySelectorAll = () => [group];
  const document = {querySelector: selector => selector === '[data-challenge-book-picker]' ? picker : selector === '[data-submission-form]' ? form : null,
    createElement: () => new Element(), addEventListener() {}};
  let timer, requests = 0;
  const context = {document, Event: class {constructor(type) {this.type=type;}},
    setTimeout: fn => {timer=fn;}, clearTimeout() {}, FormData: class {set() {}},
    fetch: async () => { requests++; return {json: async () => ({ok:true, feedback:[{domain:'tbr',message:'Possible match',suggestions:[{entry:7,title:'Death Valley',author:'Karina Halle',message:'Author differs'}]}]})}; }};
  vm.runInNewContext(fs.readFileSync('static/js/challenge_book_picker.js','utf8'), context);
  await timer();
  assert.equal(requests,1); // No prior selection is required to discover a suggestion.
  let section = feedback.children[0], box = section.children[2];
  assert.equal(box.children[2].textContent,'Request Review');
  box.children[3].click(); // Dismiss is optional and never changes selection.
  assert.equal(choice.value,''); assert.equal(section.children.length,2);
  await timer(); assert.equal(feedback.children[0].children.length,2);
  controls.author.value='Karin Halee'; // New identity may receive a new suggestion.
  await timer(); box=feedback.children[0].children[2]; box.children[2].click();
  assert.equal(choice.value,'signed'); assert.equal(controls.selected_tbr.value,'7');
  assert.equal(controls.tbr_token.value,'signed');
  assert.equal(controls.author.value,'Karin Halee'); // Request does not rewrite evidence.
  assert.equal(summary.children[2].textContent,'Remove'); summary.children[2].click();
  assert.equal(choice.value,''); assert.equal(controls.selected_tbr.value,''); assert.equal(controls.tbr_token.value,'');
  console.log('PASS: discovery, optional dismissal, repeat suppression, request review, evidence preservation and removal.');
})().catch(error => {console.error(error); process.exitCode=1;});
