/* One ephemeral Canvas composition supplies the on-screen PNG, preview and download. */
(() => {
  const root = document.querySelector('[data-wrapped]'), node = document.getElementById('wrapped-export-data');
  if (!root || !node) return;
  const data = JSON.parse(node.textContent); if (!data) return;
  const controls = root.querySelector('[data-wrapped-export]'), status = controls.querySelector('[data-export-status]');
  const buttons = [...controls.querySelectorAll('button')], slides = [...root.querySelectorAll('[data-wrapped-slide]')];
  const W=1080, H=1350, MAX=12000, GOLD='#e8cc90', IVORY='#f7f0df', MUTED='#b8b5c8';
  const images=new Map(), cache=new Map();
  const selected=()=>Math.max(0,slides.findIndex(s=>!s.hidden));
  const flags=()=>Object.fromEntries(['reader','community','team'].map(k=>[k,!!controls.querySelector(`[data-export-identity="${k}"]:checked`)]));
  function brandingSelection() {
    const options=data.branding_options||{sources:[],art:{},teams:[]};
    const source=controls.querySelector('[data-branding-source]')?.value||data.branding_selection?.branding_source||'northbound';
    if(source==='northbound')return {source,team:''};
    if(!(options.sources||[]).some(row=>row[0]===source))return null;
    if(source==='team'){
      const id=String(controls.querySelector('[data-branding-team]')?.value||'');
      const team=(options.teams||[]).find(t=>t.id===id);
      return team?{...team,source,team:id}:null;
    }
    const art=options.art?.[source];return art?{...art,source,team:''}:null;
  }
  function syncBranding(){const source=controls.querySelector('[data-branding-source]');const team=controls.querySelector('[data-branding-team]');const wrapper=controls.querySelector('[data-branding-team-control]');if(wrapper)wrapper.hidden=source?.value!=='team';if(team){team.disabled=source?.value!=='team';team.required=source?.value==='team';}}
  function image(url) {
    if(!url)return Promise.resolve(null);
    if(images.has(url))return images.get(url);
    const pending=new Promise(resolve=>{
      let u;try{u=new URL(url,location.href);}catch{resolve(null);return;}
      if(!['http:','https:'].includes(u.protocol)){resolve(null);return;}
      const i=new Image();i.crossOrigin='anonymous';i.referrerPolicy='no-referrer';
      const timer=setTimeout(()=>{i.src='';resolve(null);},5000);
      i.onload=()=>{clearTimeout(timer);resolve(i.naturalWidth&&i.naturalHeight?i:null);};
      i.onerror=()=>{clearTimeout(timer);resolve(null);};i.src=u.href;
    });images.set(url,pending);return pending;
  }
  async function render(summary,index,identity,choice) {
    const slide=data.slides[index], measure=document.createElement('canvas'), ctx=measure.getContext('2d');
    if(!ctx)throw Error('Canvas unavailable');
    const operations=[];let missing=false, y=355;
    const serif='Georgia, "Times New Roman", serif', sans='Arial, Helvetica, sans-serif';
    function rows(value,size,width,family=sans,weight='400') {
      ctx.font=`${weight} ${size}px ${family}`;
      const out=[];let line='';
      for(const word of String(value??'').split(/\s+/)) {
        if(!word)continue;
        if(line&&ctx.measureText(line+' '+word).width>width){out.push(line);line='';}
        if(line)line+=' ';
        for(const char of word){if(line&&ctx.measureText(line+char).width>width){out.push(line);line='';}line+=char;}
      }
      if(line)out.push(line);return out;
    }
    function text(value,x,top,size,width,{color=IVORY,family=sans,align='left',weight='400',maxHeight=MAX}={}) {
      let lines=rows(value,size,width,family,weight);
      while(lines.length*size*1.24>maxHeight&&size>18){size-=2;lines=rows(value,size,width,family,weight);}
      const height=lines.length*size*1.24;
      if(height>maxHeight||top+height>MAX-100)throw Error('Report too long');
      operations.push(c=>{c.font=`${weight} ${size}px ${family}`;c.fillStyle=color;c.textAlign=align;
        lines.forEach((line,i)=>c.fillText(line,x,top+size+i*size*1.24));});return height;
    }
    const number=v=>typeof v==='number'?v.toLocaleString('en-US'):String(v??'');
    function contain(img,x,top,w,h){const scale=Math.min(w/img.naturalWidth,h/img.naturalHeight);operations.push(c=>c.drawImage(img,x+(w-img.naturalWidth*scale)/2,top+(h-img.naturalHeight*scale)/2,img.naturalWidth*scale,img.naturalHeight*scale));}
    function rule(top){operations.push(c=>{c.fillStyle='#74603e';c.fillRect(78,top,924,1);});}
    const brand=identity.community&&choice?.source!=='northbound'?(choice||{}):{};
    const [northbound,banner,logo]=await Promise.all([
      image('/static/branding/northbound-header-dark.png'),image(brand.banner||''),image(brand.logo||'')]);
    operations.push(c=>{const g=c.createLinearGradient(0,0,W,320);g.addColorStop(0,'#262039');g.addColorStop(1,'#14192d');c.fillStyle=g;c.fillRect(0,0,W,320);});
    if(banner){operations.push(c=>{const s=Math.max(W/banner.naturalWidth,320/banner.naturalHeight),sw=W/s,sh=320/s;c.drawImage(banner,(banner.naturalWidth-sw)/2,(banner.naturalHeight-sh)/2,sw,sh,0,0,W,320);});}
    if(banner)operations.push(c=>{const g=c.createLinearGradient(0,0,W,320);g.addColorStop(0,'rgba(12,18,36,.78)');g.addColorStop(1,'rgba(24,16,45,.64)');c.fillStyle=g;c.fillRect(0,0,W,320);});
    if(brand.name){
      if(logo)contain(logo,280,45,520,220);else text(brand.name,W/2,70,60,900,{family:serif,color:GOLD,align:'center',maxHeight:210});
    }else text('A reading chronicle',W/2,125,44,900,{family:serif,color:GOLD,align:'center',maxHeight:100});
    const perspective={reader:'My Reading',team:'Team Wide',challenge:'Challenge Wide'}[data.perspective];
    const context=slide.context||{}, single=data.sections.length===1?data.sections[0]:context;
    function subject(s){if((data.perspective==='reader'&&identity.reader)||(data.perspective==='team'&&identity.team)){y+=text(s.subject,78,y,32,924,{color:GOLD,maxHeight:90})+12;}}
    function metrics(cards,fixed=false){
      if(fixed&&cards.length===1){const card=cards[0];y+=text(number(card.value),W/2,y,250,924,{align:'center',color:GOLD,maxHeight:310})+18;y+=text(card.label,W/2,y,36,924,{align:'center',maxHeight:100})+18;if(card.detail)y+=text(card.detail,W/2,y,26,924,{align:'center',color:MUTED,maxHeight:100});return;}
      for(let n=0;n<cards.length;n+=2){const pair=cards.slice(n,n+2),top=y;let height=0;
        for(let col=0;col<pair.length;col++){
          const card=pair[col],x=78+col*486;
          const vh=text(number(card.value),x,top,fixed&&cards.length>4?76:card.key==='entries'?140:104,438,{color:GOLD,maxHeight:150});
          const lh=text(card.label,x,top+vh+12,30,438,{maxHeight:76});
          const dh=card.detail?text(card.detail,x,top+vh+lh+30,23,438,{color:MUTED,maxHeight:fixed?65:150}):0;
          height=Math.max(height,vh+lh+dh+45);
        }y+=height+24;if(fixed&&y>1175)throw Error('Slide content too long');
      }
    }
    async function books(values,fixed=false,availableHeight=570){
      const list=values.slice(0,6),covers=await Promise.all(list.map(b=>image(b.cover_url)));
      const rowHeight=fixed?Math.min(190,Math.floor(availableHeight/Math.max(1,Math.ceil(list.length/2)))):190;
      for(let i=0;i<list.length;i++){
        const b=list[i],img=covers[i],col=fixed?i%2:0,top=fixed?y+Math.floor(i/2)*rowHeight:y,x=78+col*486;
        if(img)contain(img,x,top,100,Math.min(150,rowHeight-20));else missing=true;
        const tx=x+(img?122:0),width=(fixed?438:924)-(img?122:0);
        const h=text(b.title||'Untitled book',tx,top,34,width,{family:serif,maxHeight:fixed?Math.min(108,rowHeight-82):200});
        const a=text(b.author||'Author not recorded',tx,top+h+9,26,width,{color:MUTED,maxHeight:fixed?55:100});
        if(!fixed)y+=Math.max(img?150:0,h+a+12)+28;
      }
      if(fixed)y+=Math.ceil(list.length/2)*rowHeight;
    }
    if(summary){
      y+=text('The stories we shared.',78,y,78,924,{family:serif,maxHeight:200})+20;
      y+=text(`${perspective} · ${data.period}`,78,y,30,924,{color:MUTED,maxHeight:100})+35;
      for(const section of data.sections){
        if(data.sections.length>1){rule(y);y+=24;y+=text('Challenge recap',78,y,46,924,{family:serif,maxHeight:170})+18;}
        y+=text([section.mode,section.from_date&&`${section.from_date} – ${section.to_date}`].filter(Boolean).join(' · '),78,y,28,924,{color:MUTED,maxHeight:130})+25;
        subject(section);
        const cards=section.cards.filter(c=>['entries','pages'].includes(c.key)||c.value!==0);
        metrics(cards.filter(c=>['entries','pages'].includes(c.key)));
        const additional=cards.filter(c=>!['entries','pages'].includes(c.key));
        if(additional.length){y+=12;rule(y);y+=38;metrics(additional);}
        if(section.teams.length){y+=18;rule(y);y+=38;}
        for(const [i,team] of section.teams.entries()){y+=text(`${identity.team?team.name:'Team '+(i+1)} · ${number(team.points)} points`,78,y,32,924,{maxHeight:120})+14;}
        if(section.books.length){y+=30;rule(y);y+=34;y+=text('A few memorable chapters',78,y,48,924,{family:serif,maxHeight:130})+25;await books(section.books);}
      }
    }else{
      const opening=slide.kind==='opening';if(opening)y=460;
      const title=opening?(data.perspective==='reader'?'Your reading story':'Our reading story'):slide.kind==='summary'?'Your reading, together.':slide.title;
      y+=text(title,W/2,y,opening?100:66,opening?700:924,{family:serif,align:'center',maxHeight:opening?270:150})+18;
      y+=text(`${perspective} · ${data.period}`,W/2,y,28,924,{color:MUTED,align:'center',maxHeight:75})+16;
      const s=context.from_date?context:single;
      y+=text([slide.mode,s.from_date&&`${s.from_date} – ${s.to_date}`].filter(Boolean).join(' · '),W/2,y,26,924,{color:MUTED,align:'center',maxHeight:75})+20;
      subject(s);
      if(opening){y+=90;y+=text('A chapter of your own.',W/2,y,48,900,{family:serif,color:GOLD,align:'center',maxHeight:160})+45;y+=text('For the love of every story.',W/2,y,36,900,{family:serif,align:'center',maxHeight:160});}
      else if(slide.kind==='books')await books(slide.books||[],true,1175-y-(slide.caption?112:0));
      else if(slide.kind==='teams'){for(const [i,t] of (slide.teams||[]).entries()){y+=text(`${identity.team?t.name:'Team '+(i+1)} · ${number(t.points)} points`,78,y,36,924,{family:serif,maxHeight:66})+12;}}
      else {const cards=slide.kind==='summary'?(data.slides.filter(s=>s.kind==='overview').flatMap(s=>s.cards||[]).slice(0,4).length?data.slides.filter(s=>s.kind==='overview').flatMap(s=>s.cards||[]).slice(0,4):(single.cards||[]).slice(0,4)):(slide.cards||[]);metrics(slide.kind==='summary'?cards.filter(c=>['entries','pages'].includes(c.key)||c.value!==0):cards,true);}
      if(slide.caption)y+=text(slide.caption,78,y+12,23,924,{color:MUTED,maxHeight:100});
      if(y>1175)throw Error('Slide content too long');
    }
    const height=summary?Math.ceil(y+175):H;if(height>MAX)throw Error('Report too long');
    rule(height-135);if(northbound)contain(northbound,415,height-115,250,83);else text('NORTHBOUND',W/2,height-90,28,400,{color:GOLD,align:'center',maxHeight:50});
    const output=document.createElement('canvas');output.width=W;output.height=height;const c=output.getContext('2d');if(!c)throw Error('Canvas unavailable');
    try{const bg=c.createLinearGradient(0,0,W,height);bg.addColorStop(0,'#101425');bg.addColorStop(1,'#17162a');c.fillStyle=bg;c.fillRect(0,0,W,height);operations.forEach(draw=>draw(c));const blob=await new Promise(resolve=>output.toBlob(resolve,'image/png'));if(!blob)throw Error('PNG unavailable');return{blob,missing};}
    finally{measure.width=measure.height=0;output.width=output.height=0;}
  }
  function result(summary){const index=selected(),identity=flags(),choice=brandingSelection(),key=JSON.stringify([summary,index,identity,choice?.source,choice?.team]);if(!cache.has(key)){if(cache.size>=6)cache.delete(cache.keys().next().value);cache.set(key,render(summary,index,identity,choice).catch(e=>{cache.delete(key);throw e;}));}return cache.get(key);}
  async function download(summary){if(flags().community&&!brandingSelection()){status.textContent='Choose an authorized Team for artwork. Your recap is unchanged.';return;}buttons.forEach(b=>b.disabled=true);status.textContent='Preparing PNG…';try{const r=await result(summary),url=URL.createObjectURL(r.blob),a=document.createElement('a');a.href=url;a.download=summary?'northbound-wrapped-summary.png':'northbound-wrapped-slide.png';a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);status.textContent=r.missing?'PNG ready. Unavailable covers use compact text highlights.':'PNG ready.';}catch{status.textContent='Could not create this PNG. Try a shorter reporting period or another browser.';}finally{buttons.forEach(b=>b.disabled=false);}}
  syncBranding();controls.hidden=false;controls.querySelector('[data-export-slide]').addEventListener('click',()=>download(false));controls.querySelector('[data-export-summary]').addEventListener('click',()=>download(true));
  // No second HTML drawing path: display and enlarge the exact cached download blob.
  if(typeof MutationObserver!=='undefined'&&document.body){
    const dialog=document.createElement('dialog');dialog.className='wrapped-image-dialog';dialog.setAttribute('aria-label','Enlarged Wrapped card');
    const close=document.createElement('button');close.type='button';close.className='button secondary';close.textContent='Close preview';
    const viewport=document.createElement('div');viewport.className='wrapped-image-viewport';viewport.tabIndex=0;viewport.setAttribute('aria-label','Scrollable full-resolution card');const large=document.createElement('img');large.alt='Full-resolution Wrapped card';viewport.append(large);dialog.append(close,viewport);document.body.append(dialog);
    let opener=null,version=0,activeUrl='';close.addEventListener('click',()=>dialog.close());dialog.addEventListener('click',e=>{if(e.target===dialog)dialog.close();});dialog.addEventListener('close',()=>{const target=root.querySelector('.wrapped-png-button')||opener;target?.focus();});
    dialog.addEventListener('keydown',e=>{if(e.target===viewport)return;if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();root.querySelector(e.key==='ArrowRight'?'[data-wrapped-next]':'[data-wrapped-prev]').click();}});
    const numberForDescription=v=>typeof v==='number'?v.toLocaleString('en-US'):String(v??'');
    async function preview(){const current=++version,article=slides[selected()];
      // Never leave a previously consented image visible while new state renders.
      root.querySelectorAll('.wrapped-png-button').forEach(b=>b.remove());
      root.querySelectorAll('.wrapped-slide-inner').forEach(e=>e.hidden=true);
      large.removeAttribute('src');if(activeUrl){URL.revokeObjectURL(activeUrl);activeUrl='';}
      status.textContent='Preparing card…';
      try{const r=await result(false);if(current!==version)return;const url=URL.createObjectURL(r.blob);if(activeUrl)URL.revokeObjectURL(activeUrl);activeUrl=url;
      root.querySelectorAll('.wrapped-png-button').forEach(b=>b.remove());root.querySelectorAll('.wrapped-slide-inner').forEach(e=>e.hidden=false);
      const button=document.createElement('button');button.type='button';button.className='wrapped-png-button';button.setAttribute('aria-label','Enlarge Wrapped card');button.setAttribute('aria-haspopup','dialog');const img=document.createElement('img');img.src=url;img.alt=`Wrapped card ${selected()+1} of ${slides.length}`;img.width=1080;img.height=1350;button.append(img);
      const source=data.slides[selected()],description=document.createElement('span');description.className='visually-hidden';description.id='wrapped-png-description-'+selected();
      description.textContent=[source.title,...(source.cards||[]).map(c=>`${c.label}: ${numberForDescription(c.value)}`),...(source.books||[]).map(b=>`${b.title}, ${b.author}`),source.caption].filter(Boolean).join('. ');
      button.setAttribute('aria-describedby',description.id);button.append(description);article.prepend(button);article.querySelector('.wrapped-slide-inner').hidden=true;large.src=url;
      button.addEventListener('click',()=>{opener=button;dialog.showModal();close.focus();});status.textContent=flags().community&&!brandingSelection()?'Choose a Team for artwork. Northbound branding is shown until then.':'Card ready. Select the image to enlarge it.';
    }catch{if(current===version){article.querySelector('.wrapped-slide-inner').hidden=false;status.textContent='Image preview unavailable. The accessible recap remains below.';}}}
    new MutationObserver(records=>{if(records.some(r=>r.target.matches('[data-wrapped-slide]')))preview();}).observe(root,{subtree:true,attributes:true,attributeFilter:['hidden']});
    controls.addEventListener('change',()=>{syncBranding();preview();});preview();
    window.addEventListener('pageshow',e=>{if(e.persisted){document.body.append(dialog);preview();}});
    window.addEventListener('pagehide',()=>{if(activeUrl)URL.revokeObjectURL(activeUrl);dialog.remove();});
  }
})();
