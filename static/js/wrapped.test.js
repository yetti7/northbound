const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/wrapped.js', 'utf8');
function setup(count = 3) {
  const handlers = {}, stageHandlers = {};
  const previous = {disabled: true, addEventListener(type, cb) {handlers.previous = cb;}};
  const next = {disabled: false, addEventListener(type, cb) {handlers.next = cb;}};
  const status = {textContent: ''}, progress = {value: 0}, controls = {hidden: true};
  const image = {dataset: {coverSrc: 'https://covers.example.test/approved.jpg'}, alt: 'Cover of Approved book',
    closest() {return {};}, removeAttribute(name) {delete this.dataset.coverSrc;}};
  const slides = Array.from({length: count}, (_, index) => ({hidden: false, focused: false,
    querySelectorAll() {return index === 1 && image.dataset.coverSrc ? [image] : [];},
    focus() {this.focused = true;}}));
  const stage = {scrollTop: 0, addEventListener(type, cb) {stageHandlers[type] = cb;}};
  const root = {querySelectorAll() {return slides;},setAttribute() {},
    querySelector(selector) {return {'[data-wrapped-stage]':stage,'[data-wrapped-prev]':previous,
      '[data-wrapped-next]':next,'[data-wrapped-status]':status,'[data-wrapped-progress]':progress,
      '[data-wrapped-controls]':controls}[selector];},
    addEventListener(type, cb) {handlers[type] = cb;}};
  const loads=[];
  vm.runInNewContext(source, {document: {querySelectorAll() {return [root];}},
    window: {NorthboundBookCovers: {update(...args) {loads.push(args);}}}});
  function key(key, editable = false, options = {}) {
    let prevented = false;
    handlers.keydown({key, target: {closest() {return editable;}}, preventDefault() {prevented=true;},...options});
    return prevented;
  }
  function swipe(dx,dy=0,options={}) {
    stageHandlers.pointerdown({pointerType:'touch',isPrimary:true,pointerId:1,clientX:150,clientY:100,timeStamp:0,...options});
    stageHandlers.pointerup({pointerId:1,clientX:150+dx,clientY:100+dy,timeStamp:100});
  }
  return {slides,handlers,stageHandlers,previous,next,status,progress,controls,key,swipe,loads};
}
let state = setup();
assert.deepEqual(state.slides.map(s=>s.hidden),[false,true,true]);
assert.equal(state.controls.hidden,false);assert.equal(state.status.textContent,'Slide 1 of 3');
assert.equal(state.previous.disabled,true);assert.equal(state.loads.length,0);
state.handlers.next();assert.equal(state.progress.value,2);assert.equal(state.loads.length,1);
assert.equal(state.slides[1].focused,true);
state.handlers.previous();state.handlers.next();assert.equal(state.loads.length,1); // Already loaded covers are reused.
state.key('End');assert.equal(state.next.disabled,true);assert.equal(state.progress.value,3);
state.key('ArrowRight');assert.equal(state.progress.value,3);
state.key('Home');assert.equal(state.progress.value,1);
state.key('ArrowLeft');assert.equal(state.progress.value,1);
assert.equal(state.key('ArrowRight',true),false);assert.equal(state.progress.value,1);
assert.equal(state.key('ArrowRight',false,{ctrlKey:true}),false);assert.equal(state.progress.value,1);
assert.equal(state.key('ArrowRight'),true);assert.equal(state.progress.value,2);
state=setup();state.swipe(-100);assert.equal(state.progress.value,2);
state.swipe(100);assert.equal(state.progress.value,1);
state.swipe(-10);assert.equal(state.progress.value,1);
state.swipe(-100,150);assert.equal(state.progress.value,1); // Vertical scrolling is preserved.
state.swipe(-100,0,{pointerType:'mouse'});assert.equal(state.progress.value,1);
state.swipe(-100,0,{isPrimary:false});assert.equal(state.progress.value,1);
state.stageHandlers.pointerdown({pointerType:'touch',isPrimary:true,pointerId:1,clientX:100,clientY:0,timeStamp:0});
state.stageHandlers.pointercancel();state.stageHandlers.pointerup({pointerId:1,clientX:0,clientY:0,timeStamp:100});
assert.equal(state.progress.value,1);
state=setup(1);assert.equal(state.previous.disabled,true);assert.equal(state.next.disabled,true);
setup(0); // Empty pages do not initialize a carousel.
const css=fs.readFileSync(__dirname+'/../css/wrapped.css','utf8');
assert.match(css,/@media\(prefers-reduced-motion:reduce\)/);assert.match(css,/animation:none/);
assert.match(css,/touch-action:pan-y pinch-zoom/);
console.log('Wrapped navigation checks passed: boundaries, Previous/Next, keys, focus, touch swipe, scroll/cancel safety, progress, deferred covers, empty/single slides, and reduced-motion CSS.');
