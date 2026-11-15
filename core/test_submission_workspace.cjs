const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const author={name:'author',value:'Original'}, edition={name:'edition',value:''}, csrf={name:'csrfmiddlewaretoken',value:'token'};
function form(elements) { return {elements,listeners:{},bound:false,addEventListener(name,fn){this.listeners[name]=fn;},hasAttribute(){return this.bound;}}; }
const first=form([author,edition,csrf]), second=form([{name:'reason',value:''}]);
const events={};
vm.runInNewContext(fs.readFileSync('static/js/submission_workspace.js','utf8'),{
  document:{querySelector:()=>({querySelectorAll:()=>[first,second]})},window:{addEventListener:(name,fn)=>events[name]=fn}
});
function unload() {let prevented=false;events.beforeunload({preventDefault(){prevented=true;},returnValue:undefined});return prevented;}
assert.equal(unload(),false);
csrf.value='rotated';assert.equal(unload(),false);
author.value='Changed';assert.equal(unload(),true);
first.listeners.submit();assert.equal(unload(),false);assert.equal(unload(),true);
author.value='Original';edition.value='42';assert.equal(unload(),true);
second.elements[0].value='Separate operation';first.listeners.submit();assert.equal(unload(),true);
second.elements[0].value='';edition.value='';first.bound=true;assert.equal(unload(),true);
second.listeners.submit();assert.equal(unload(),true);first.listeners.submit();assert.equal(unload(),false);
console.log('PASS: ordinary and hidden-field dirtiness, CSRF rotation, preview/error state, own-submit exemption and independent forms.');
