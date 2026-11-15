const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function field(props={}) { return {...props,disabled:false,listeners:{},addEventListener(event,fn){this.listeners[event]=fn;}}; }
const enabled=field({checked:false}),frequency=field({value:'disabled'}),time=field(),day=field(),empty=field();
const controls={hidden:false,querySelectorAll(){return [frequency,time,day,empty];}};
const times={hidden:false,querySelectorAll(){return [time];}};
const days={hidden:false,querySelectorAll(){return [day];}};
const form={elements:{namedItem(name){return name==='scheduled_enabled'?enabled:frequency;}}};
const panel={querySelector(selector){return {'[data-report-form]':form,'[data-frequency-controls]':controls,'[data-fixed-times]':times,'[data-weekdays]':days}[selector];}};
vm.runInNewContext(fs.readFileSync('static/js/discord_scheduling.js','utf8'),{document:{querySelectorAll(){return [panel];}}});
assert(controls.hidden && frequency.disabled && times.hidden && days.hidden);
enabled.checked=true;enabled.listeners.change();assert.equal(frequency.value,'1h');
assert(!controls.hidden && !frequency.disabled && !empty.disabled && times.hidden && days.hidden);
frequency.value='daily';frequency.listeners.change();assert(!times.hidden && !time.disabled && days.hidden && day.disabled);
for(const value of ['weekly','custom']) {frequency.value=value;frequency.listeners.change();assert(!times.hidden && !days.hidden && !day.disabled);}
frequency.value='disabled';frequency.listeners.change();assert(!enabled.checked && controls.hidden && frequency.disabled && day.disabled);
console.log('Discord schedule controls: disabled, interval, daily, weekly and custom visibility passed.');
