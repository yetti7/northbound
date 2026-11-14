const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/wrapped_export.js', 'utf8');
async function check(perspective, opts = {}, fail = false, cover = false, long = false) {
  const state={source:opts.source||'group',team:'1'}, teamControl={value:'1'}, wrapper={};
  const positions=[], draws = [], handlers = {}, downloads = [], canvases = [], sizes = [], artwork = [];
  const context = {createLinearGradient:()=>({addColorStop(){}}),fillRect(){},drawImage(img,x,y,w,h){artwork.push({w,h,url:img._url});},
    measureText:s=>({width:s.length * 12}), fillText:(s,x,y)=>{draws.push(s);positions.push({text:s,x,y});}};
  const controls = {hidden:true,querySelectorAll:()=>buttons,querySelector(selector){
    if(selector === '[data-export-status]') return status;
    if(selector === '[data-branding-source]')return {value:state.source};
    if(selector === '[data-branding-team]')return teamControl;
    if(selector === '[data-branding-team-control]')return wrapper;
    if(selector.includes(':checked')) return Object.keys(opts).some(k=>opts[k] && selector.includes(`"${k}"`)) ? {} : null;
    return {addEventListener:(event,cb)=>{handlers[selector]=cb;}};
  }};
  const buttons = [{disabled:false},{disabled:false}], status = {textContent:''};
  const section = {challenge:'November',mode:'Final Wrapped',subject:perspective==='team'?'PRIVATE_TEAM':'PRIVATE_USER',group:'PRIVATE_GROUP',
    cards:[{key:'entries',label:'Approved books',value:12},{key:'pages',label:'Recorded pages',value:4321},{key:'zero',label:'Empty award',value:0}], books:[{title:long?'Long approved book title '+ 'UnbrokenTitle'.repeat(20):'Long approved book title',author:'Author',cover_url:cover?'/cover.png':''}],teams:[{name:'PRIVATE_TEAM',points:10}]};
  if(opts.missingTitle){section.books[0].title='';section.books[0].author='';}
  if(opts.sixBooks)section.books=Array.from({length:6},(_,i)=>({...section.books[0],title:section.books[0].title+' '+(i+1)}));
  const data = {perspective,period:'November',branding_options:{sources:[['northbound','Northbound Default'],['group','Group Branding'],['challenge','Challenge Branding'],['team','Team Branding']],art:{group:{name:'PRIVATE_GROUP',logo:opts.art?.logo||'',banner:opts.art?.banner||''},challenge:{name:'PRIVATE_CHALLENGE',logo:'/art-challenge',banner:'/art-challenge-banner'}},teams:[{id:'1',name:'PRIVATE_ART_TEAM',logo:opts.teamFallback?'':'/art-team1',banner:''},{id:'2',name:'PRIVATE_ART_TEAM2',logo:'/art-team2',banner:''}]},sections:[section],slides:[
    {kind:'reading',title:'First slide',context:section,cards:[{label:'First',value:1}]},
    {kind:opts.sixBooks?'books':opts.opening?'opening':opts.reading?'reading':opts.sixContributions?'contributions':opts.summarySlide?'summary':'teams',title:'Selected slide',context:section,books:opts.sixBooks?section.books:undefined,teams:section.teams,cards:opts.reading?section.cards.filter(c=>c.key!=='zero'):opts.sixContributions?Array.from({length:6},(_,i)=>({label:'Reader contribution',value:i+1})):undefined}]};
  const root = {querySelector:()=>controls,querySelectorAll:()=>[{hidden:true},{hidden:false}]};
  const document = {querySelector:()=>root,getElementById:()=>({textContent:JSON.stringify(data)}),createElement(tag){
    if(tag==='a') {const a={click(){downloads.push(this.download);}};return a;}
    const canvas={width:0,height:0,getContext:()=>fail ? null : context,toBlob:cb=>{sizes.push({width:canvas.width,height:canvas.height});cb({type:'image/png'});}};canvases.push(canvas);return canvas;
  }};
  class Image { set src(value) {this._url=value?new URL(value).pathname:"";if(value) queueMicrotask(()=>{if(this._url.startsWith('/art-')){this.naturalWidth=800;this.naturalHeight=400;this.onload();}else if(value.endsWith('/cover.png')){this.naturalWidth=200;this.naturalHeight=300;this.onload();}else this.onerror();});} }
  vm.runInNewContext(source,{document,Image,URL:Object.assign(URL,{createObjectURL:()=> 'blob:test',revokeObjectURL(){}}),location:{href:'http://localhost/'},setTimeout:()=>1,clearTimeout(){}});
  assert.equal(controls.hidden,false);
  await handlers['[data-export-slide]']();
  if(fail) {assert.match(status.textContent,/Could not/);assert.deepEqual(downloads,[]);assert.ok(buttons.every(b=>!b.disabled));return;}
  assert.ok(draws.some(s=>s.includes(opts.opening?'reading story':opts.summarySlide?'Your reading, together.':'Selected slide')));assert.ok(!draws.some(s=>s.includes('First slide')));
  await handlers['[data-export-summary]']();
  assert.deepEqual(downloads,['northbound-wrapped-slide.png','northbound-wrapped-summary.png']);
  assert.ok(draws.some(s=>s.includes(opts.missingTitle?'Untitled book':'Long approved book title')));if(opts.missingTitle)assert.ok(draws.includes('Author not recorded'));
  for(const [key,text] of [['reader','PRIVATE_USER'],['team','PRIVATE_TEAM']]) {
    const allowed=opts[key] && (key!=='reader'||perspective==='reader');
    assert.equal(draws.some(s=>s.includes(text)),!!allowed,`${perspective}: ${key}`);
  }
  assert.equal(draws.some(s=>s.includes('PRIVATE_GROUP')),!!opts.community&&state.source==='group'&&!opts.art?.logo);
  assert.ok(buttons.every(b=>!b.disabled));assert.ok(canvases.every(c=>c.width===0&&c.height===0));
  assert.ok(draws.some(s=>s.includes(' \u00b7 ')));assert.ok(!draws.some(s=>s.includes('Â')));
  assert.ok(!draws.some(s=>s.includes('Empty award')));
  assert.equal(sizes[0].width,1080);assert.equal(sizes[0].height,1350);assert.equal(sizes[1].width,1080);
  if(cover) {assert.ok(artwork.length); assert.ok(artwork.filter(a=>a.url==='/cover.png').every(a=>Math.abs(a.w/a.h-2/3)<0.001));}
  else if(!opts.art&&!opts.community)assert.equal(artwork.length,0);
  if(opts.art&&opts.community){assert.ok(artwork.some(a=>a.url===opts.art.logo));if(opts.art.banner)assert.ok(artwork.some(a=>a.url===opts.art.banner));}
  if(opts.switches){
    const count=sizes.length;
    for(const [source,team,expected] of [['challenge','1','/art-challenge'],['team','1','/art-team1'],['team','2','/art-team2']]){
      state.source=source;teamControl.value=team;const before=artwork.length;await handlers['[data-export-slide]']();assert.ok(artwork.slice(before).some(a=>a.url===expected));
    }
    assert.equal(sizes.length,count+3,'source and Team ID must partition the cache');
    opts.community=false;const before=artwork.length;await handlers['[data-export-slide]']();assert.equal(artwork.length,before,'consent off draws no community artwork');
    const cached=sizes.length;opts.community=true;await handlers['[data-export-slide]']();assert.equal(sizes.length,cached,'cached selected render reused');
    state.source='team';teamControl.value='FORGED';await handlers['[data-export-slide]']();assert.match(status.textContent,/authorized Team/);
  }
  if(opts.teamFallback)assert.ok(draws.includes('PRIVATE_ART_TEAM'),'community consent explicitly includes selected Team identity even when report-name checkbox is off');
  if(opts.opening){const closing=positions.find(p=>p.text==='For the love of every story.');assert.ok(closing.y>900&&closing.y<1175,'opening copy balanced above fixed footer');}
  return sizes[1].height;
}
(async()=>{for(const perspective of ['reader','team','challenge']) await check(perspective);
await check('reader',{reader:true,community:true});await check('team',{team:true,community:true});await check('reader',{},true);
const compact=await check('reader');const illustrated=await check('reader',{},false,true);const long=await check('reader',{},false,false,true);
assert.ok(illustrated>compact,'missing covers must collapse image height');assert.ok(long>compact,'long titles must grow rather than clip');assert.notEqual(compact,1350,'summary height must remain content-driven rather than fixed');assert.ok(compact<1700,'short reports remain compact after section spacing');
assert.match(fs.readFileSync(__dirname+'/../../templates/base.html','utf8'),/<meta charset="utf-8">/);
assert.ok(!source.includes('Â'));assert.ok(source.includes('·'));
await check('challenge',{community:true,art:{logo:'/art-group',banner:'/art-group-banner'}});
await check('challenge',{community:true,source:'challenge'});
await check('challenge',{community:true,source:'team',summarySlide:true});
await check('challenge',{community:true,art:{logo:'/art-group',banner:''},switches:true});
await check('team',{sixContributions:true});
await check('team',{source:'team',community:true,teamFallback:true});await check('reader',{reader:true});await check('team',{team:true});
await check('reader',{missingTitle:true});await check('reader',{opening:true});await check('team',{reading:true});
for(const reader of [false,true])for(const community of [false,true])for(const cover of [false,true])await check('reader',{sixBooks:true,reader,community},false,cover);
console.log('28 export scenarios plus source/team/consent cache transitions passed: compact/valid covers, long titles, content height, UTF-8 separator, perspectives, privacy defaults, selected slide/summary and failures.');})().catch(e=>{console.error(e);process.exitCode=1;});
