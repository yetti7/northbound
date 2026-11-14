const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/reader_rumble.js','utf8');
async function check(top,count,{missing=false,fail=false,long=false,changeEarly=false,unicode=false,art='both'}={}){
  const drawn=[],images=[],downloads=[],handlers={},sizes=[],created=[],revoked=[],panels=[],artwork=[];
  let submits=0;
  const data={team:long?'A very long Team name '.repeat(7):'Team A',challenge:long?'A wonderfully long Challenge title '.repeat(6):'November Challenge',top,metric:'Total Challenge Points',timezone:'America/New_York',filename:'reader-rumble-november-team-a',logo:missing||['none','banner'].includes(art)?'':art==='broken'?'/broken-logo.png':'/logo.png',banner:missing||['none','logo'].includes(art)?'':art==='broken'?'/broken-banner.png':'/banner.png',rows:Array.from({length:count},(_,i)=>({rank:i+1,username:unicode?'𐐀漢字'.repeat(50):long?'Reader_'+i+'x'.repeat(145):'Reader '+i,value:count===3?0:long?1234567890123-i:10000-i}))};
  function element(tag){const e={tag,children:[],attrs:{},listeners:{},disabled:false,append(...items){this.children.push(...items);},setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this[k];},addEventListener(k,fn){this.listeners[k]=fn;},focus(){this.focused=true;},showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.();}};created.push(e);return e;}
  const status={textContent:''},button=element('download');button.disabled=true;
  const form={addEventListener:(event,callback)=>handlers.change=callback,requestSubmit:()=>submits++};
  const target={replaceChildren(...items){images.splice(0,images.length,...items);}};
  const context={font:'',textAlign:'left',measureText(s){return{width:Array.from(s).length*Number((this.font.match(/([\d.]+)px/)||[0,24])[1])*.58};},fillText(value,x,y){drawn.push({value,x,y,font:Number((this.font.match(/([\d.]+)px/)||[0,24])[1]),align:this.textAlign,width:this.measureText(value).width});},fillRect(){},beginPath(){},roundRect(x,y,w,h){panels.push({x,y,w,h});},fill(){},stroke(){},arc(){},moveTo(){},lineTo(){},drawImage(image,...args){artwork.push({src:image._src,args});},createLinearGradient:()=>({addColorStop(){}})};
  const document={body:{append(){}},getElementById:()=>({textContent:JSON.stringify(data)}),querySelector:sel=>sel.includes('controls')?form:sel.includes('download')?button:sel.includes('status')?status:target,createElement:tag=>{
    if(tag==='canvas'){const c={width:0,height:0,getContext:()=>fail?null:context,toBlob:callback=>{sizes.push([c.width,c.height]);callback({type:'image/png'});}};return c;}
    if(tag==='a'){const a={click(){downloads.push({href:a.href,filename:a.download});}};return a;}
    return element(tag);
  }};
  class Image{set src(value){if(!value)return;this._src=value;queueMicrotask(()=>{if(missing||value.includes('broken'))this.onerror();else{this.naturalWidth=800;this.naturalHeight=200;this.onload();}});}}
  class FixedDate extends Date{constructor(){super('2026-11-14T02:15:00Z');}}
  vm.runInNewContext(source,{document,Image,URL:Object.assign(URL,{createObjectURL:()=> 'blob:exact-fixed-png',revokeObjectURL:value=>revoked.push(value)}),Date:FixedDate,Intl,location:{href:'http://localhost/'},setTimeout:()=>1,clearTimeout(){},window:{addEventListener:(event,callback)=>handlers[event]=callback}});
  if(!count){assert.ok(button.disabled);assert.equal(images.length,0);assert.equal(sizes.length,0);assert.match(status.textContent,/No Readers match/);handlers.change();assert.equal(submits,1);return;}
  if(changeEarly)handlers.change();
  for(let n=0;n<25;n++)await Promise.resolve();
  if(changeEarly){assert.equal(images.length,0);assert.ok(button.disabled);assert.equal(submits,1);return;}
  if(fail){assert.match(status.textContent,/Could not/);assert.ok(button.disabled);assert.equal(images.length,0);return;}
  assert.equal(button.disabled,false);assert.equal(images.length,1);assert.deepEqual(sizes[0],[1080,1350]);
  assert.ok(drawn.some(row=>row.value==='READER RUMBLE'));assert.ok(drawn.some(row=>row.value.includes('EST')));
  assert.ok(drawn.some(row=>row.value.includes('Nov 13, 2026')));
  const opener=images[0],preview=opener.children[0],dialog=created.find(e=>e.tag==='dialog'),large=created.filter(e=>e.tag==='img').find(e=>e!==preview);
  opener.listeners.click();assert.ok(dialog.open);assert.equal(large.src,preview.src);
  created.find(e=>e.textContent==='Close preview').listeners.click();assert.ok(!dialog.open);assert.ok(opener.focused);
  button.listeners.click();assert.equal(downloads[0].href,preview.src,'preview/enlarge/download use same PNG bytes');
  assert.equal(downloads[0].filename,'reader-rumble-november-team-a-2026-11-13.png');
  assert.ok(drawn.every(row=>row.y<=1330&&row.y-row.font>=0),'all text vertically bounded');
  assert.ok(drawn.every(row=>(row.align==='right'?row.x-row.width:row.x)>=0&&(row.align==='right'?row.x:row.x+row.width)<=1080),'all text horizontally bounded');
  if(top===5&&count>=3){const cards=panels.filter(p=>p.y>=430&&p.y<1200);assert.equal(cards.length,count);assert.ok(cards[0].h>cards[1].h);assert.equal(cards[1].h,cards[2].h);if(count===5)assert.ok(cards[2].h>cards[3].h);}
  assert.deepEqual(drawn.filter(r=>r.align==='right').map(r=>r.value),data.rows.map(r=>Number(r.value).toLocaleString('en-US')),'displayed totals and row order are unchanged');
  for(const image of artwork.filter(a=>a.args.length===4))assert.ok(Math.abs(image.args[2]/image.args[3]-4)<.001,'contain preserves logo aspect ratio');
  const bannerDraw=artwork.find(a=>a.src.endsWith('/banner.png'));if(bannerDraw){assert.equal(bannerDraw.args.length,8);assert.equal(bannerDraw.args[1],0,'banner source is top-anchored');assert.ok(Math.abs(bannerDraw.args[2]/bannerDraw.args[3]-1080/520)<.001,'cover crop preserves source proportions');}
  if(count===3)assert.equal(drawn.filter(row=>row.align==='right'&&row.value==='0').length,3,'zero-credit Readers stay visible');
  const split=top===25?13:10;
  const ranks=drawn.filter(row=>row.y>430&&row.y<1200&&/^\d+$/.test(row.value)&&[68,572,105].includes(row.x));
  assert.equal(ranks.length,count,'no placeholder rows');
  for(let i=0;i<count;i++){const rank=ranks[i];assert.equal(rank.value,String(i+1));assert.equal(rank.x,top===5&&i<3?105:top>=20&&i>=split?572:68);}
  for(const row of drawn.filter(row=>count>0&&row.y>430&&row.y<1200&&![68,572,105].includes(row.x))){
    if(row.align==='right')continue;
    const max=top>=20?(row.x<560?388:892):800;
    assert.ok(row.x+row.width<=max+2,'username must not reach reserved numeric region '+JSON.stringify({top,count,row,max}));
  }
  assert.ok(drawn.every(row=>Array.from(row.value).every(char=>char.codePointAt(0)<0xD800||char.codePointAt(0)>0xDFFF)),'truncation preserves Unicode characters');
  if(long&&count)assert.ok(drawn.some(row=>row.value.endsWith('…')),'long names truncate deliberately');
  handlers.pagehide({persisted:true});button.listeners.click();assert.equal(downloads.length,2);
  handlers.change({target:{name:'include_zero'}});assert.equal(images.length,0);assert.ok(button.disabled);assert.equal(large.src,undefined);assert.equal(submits,1);assert.ok(revoked.includes(preview.src));
  button.listeners.click();assert.equal(downloads.length,2,'dirty settings cannot download stale PNG');
  handlers.pageshow({persisted:true});assert.equal(submits,2,'cached invalidated page regenerates');
}
async function emptyPage(){let refresh=0,listener;vm.runInNewContext(source,{document:{getElementById:()=>null,querySelector:()=>({addEventListener:(event,cb)=>listener=cb,requestSubmit:()=>refresh++})}});listener({target:{name:'include_zero'}});assert.equal(refresh,1);}
(async()=>{await emptyPage();let count=0;for(const top of [5,10,15,20,25])for(const missing of [false,true])for(const n of [top,3,0]){await check(top,n,{missing,long:n===top});count++;}await check(25,25,{changeEarly:true});await check(5,5,{fail:true});await check(25,25,{long:true,unicode:true});for(const top of [5,10,15,20,25])for(const art of ['none','logo','banner','broken'])await check(top,top,{art});console.log(`${count+24} presentation/zero-filter Canvas cases passed: all Top N/column splits, full/fewer/empty, long names and numbers, branding/fallback, 1080x1350, bounded text, same preview/enlarge/download blob, timestamp, stale settings and in-flight render cancellation.`);})().catch(e=>{console.error(e);process.exitCode=1;});
