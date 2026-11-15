const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const listenerObject = props => ({...props,listeners:{},addEventListener(event,fn){this.listeners[event]=fn;}});
const panels={community:{hidden:false},moderator:{hidden:false}};
const tabs=['community','moderator'].map((key,index)=>listenerObject({dataset:{discordTab:key},attrs:{'aria-controls':key,'aria-selected':String(index===0)},getAttribute(name){return this.attrs[name];},setAttribute(name,value){this.attrs[name]=value;},focus(){this.focused=true;}}));
const forms=['community','moderator'].map(key=>listenerObject({dataset:{formErrors:'false'},values:[['destination',key],['times','08:00']]}));
const root={querySelectorAll(selector){return selector==='form'?forms:tabs;}};
const win={location:{href:'https://northbound.example/settings/'},history:{replaceState(_,__,url){win.location.href=String(url);}},listeners:{},addEventListener(event,fn){this.listeners[event]=fn;}};
class FormData {constructor(form){this.form=form;}*[Symbol.iterator](){yield* this.form.values;}}
vm.runInNewContext(fs.readFileSync('static/js/discord_tabs.js','utf8'),{document:{querySelector(){return root;},getElementById(id){return panels[id];}},window:win,URL,FormData});
assert(!panels.community.hidden && panels.moderator.hidden);assert.equal(tabs[0].tabIndex,0);assert.equal(tabs[1].tabIndex,-1);
forms[0].values[1][1]='17:00';forms[0].listeners.input();
tabs[1].listeners.click();assert(panels.community.hidden && !panels.moderator.hidden);assert.equal(forms[0].values[1][1],'17:00');assert(win.location.href.endsWith('destination=moderator'));
let prevented=false;
tabs[1].listeners.keydown({key:'ArrowLeft',preventDefault(){prevented=true;}});assert(prevented && tabs[0].focused && !panels.community.hidden);
tabs[0].listeners.keydown({key:'End',preventDefault(){}});assert(tabs[1].focused && !panels.moderator.hidden);
tabs[1].listeners.keydown({key:'Home',preventDefault(){}});assert(!panels.community.hidden);
function warns(){let warned=false;win.listeners.beforeunload({preventDefault(){warned=true;}});return warned;}
assert(warns());forms[0].values[1][1]='08:00';forms[0].listeners.change();assert(!warns());
forms[0].values[1][1]='18:00';forms[0].listeners.input();forms[1].values[1][1]='19:00';forms[1].listeners.change();
forms[0].listeners.submit();assert(warns());forms[1].listeners.submit();assert(!warns());
console.log('Discord tabs: ARIA state, arrows/Home/End, retained edits and unload warnings passed.');
