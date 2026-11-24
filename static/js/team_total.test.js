const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function fixture(rows,query='') {
 const input={value:query,addEventListener(name,fn){this.changed=fn;}};
 const clear={addEventListener(name,fn){this.update=fn;},click(){input.value='';this.update();}};
 const total={textContent:'1,660'};
 const search={querySelector(selector){return selector==='input'?input:clear;}};
 const reset={addEventListener(name,fn){this.click=fn;}};
 vm.runInNewContext(fs.readFileSync('static/js/team_roster.js','utf8'),{Intl,document:{querySelector(selector){return {'[data-team-total]':total,'[data-participant-search]':search,'.filter-actions a':reset}[selector]||null;},getElementById(){return rows===null?null:{textContent:JSON.stringify(rows)};}}});
 return {input,clear,total,reset};
}
const rows=[{names:['Alpha Reader','AlphaDiscord'],pages:360},{names:['Beta Reader'],pages:340},{names:['Historical Reader'],pages:1000}];
let f=fixture(rows);assert.equal(f.total.textContent,'1,700');
for(const [query,total] of [['alpha','360'],['ALPHADISCORD','360'],['historical','1,000'],['no match','0'],[' reader ','1,700']]){f.input.value=query;f.input.changed();assert.equal(f.total.textContent,total);}
f=fixture(rows,'alpha');assert.equal(f.total.textContent,'360');f.clear.click();assert.equal(f.total.textContent,'1,700');
f.input.value='beta';f.input.changed();f.reset.click();assert.equal(f.input.value,'');assert.equal(f.total.textContent,'1,700');
f=fixture(null,'alpha');assert.equal(f.total.textContent,'—');f.clear.click();assert.equal(f.total.textContent,'1,660');
f=fixture([]);assert.equal(f.total.textContent,'0');
console.log('PASS: live/persisted Reader search, historical credit, empty results, reset, formatting and standings-only privacy.');
