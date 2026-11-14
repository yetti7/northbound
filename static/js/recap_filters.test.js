const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname+'/recap_filters.js','utf8');
for (const changed of ['group','challenge','perspective']) {
  const handlers = {}, fields={}; let submissions=0;
  for(const key of ['group','challenge','perspective','reader','team','period']) fields[key]={value:'selected',addEventListener(event,callback){handlers[key]=callback;}};
  const form={elements:{namedItem(name){return fields[name];}},requestSubmit(){submissions++;}};
  vm.runInNewContext(source,{document:{querySelector(){return form;}}});
  handlers[changed]();
  assert.equal(submissions,1);assert.equal(fields.reader.value,'');assert.equal(fields.team.value,'');
  assert.equal(fields.period.value,'selected');
  assert.equal(fields.challenge.value,changed==='group'?'':'selected');
}
vm.runInNewContext(source,{document:{querySelector(){return null;}}});
console.log('Dependent filters refresh, stale targets clear, periods persist; passed.');
