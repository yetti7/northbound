const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const status = {value:'approved'}, botm = {checked:false}, tbr = {checked:false}, button = {};
const form = {events:{},addEventListener(name,callback){this.events[name]=callback;},querySelector(selector){
  return {'[name="status"]':status,'[name="confirm_botm"]':botm,'[name="confirm_tbr"]':tbr,'[data-review-submit]':button}[selector];
}};
vm.runInNewContext(fs.readFileSync(__dirname+'/submission_review.js','utf8'),{document:{querySelector:()=>form}});
assert.equal(button.textContent,'Approve Book');
botm.checked=true;form.events.change();assert.equal(button.textContent,'Approve Book + Confirm BOTM');
tbr.checked=true;form.events.change();assert.equal(button.textContent,'Approve Book + Confirm BOTM & TBR');
botm.checked=false;form.events.change();assert.equal(button.textContent,'Approve Book + Confirm TBR');
status.value='rejected';form.events.change();assert.equal(button.textContent,'Save Review');
assert.equal(tbr.checked,true);
console.log('Unified approval label checks passed.');
