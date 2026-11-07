const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const handlers = {};
const form = {elements: {submitted_pages: {value:'673'}, immersive_request: {checked:true}},
  addEventListener: (name, handler) => {handlers[name]=handler;}};
const preview = {dataset:{percent:'100'},textContent:'',closest:()=>form};
const document = {querySelector:()=>preview,addEventListener:(name,handler)=>{handlers[name]=handler;}};
vm.runInNewContext(fs.readFileSync(__dirname+'/submission_scoring_preview.js','utf8'),{document});
assert.match(preview.textContent,/673.*\+673.*1,346/);
assert.match(preview.textContent,/Estimate excludes other bonuses/);
preview.dataset.percent='50';form.elements.submitted_pages.value='351';handlers.input();
assert.match(preview.textContent,/\+176.*527/);
form.elements.immersive_request.checked=false;handlers.change();assert.match(preview.textContent,/\+0.*351/);
form.elements.submitted_pages.value='';handlers['northbound-pages-selected']();assert.match(preview.textContent,/Enter pages/);
form.elements.submitted_pages.value='2147483648';handlers.input();assert.match(preview.textContent,/Enter pages/);
console.log('PASS: provisional Immersive basis, round-half-up, no compounding, deselection, and invalid pages');
