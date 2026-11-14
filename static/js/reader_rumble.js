/* One fixed-size Canvas PNG supplies preview, enlargement and download. */
(() => {
  const node=document.getElementById('reader-rumble-data'),form=document.querySelector('[data-rumble-controls]');
  if(!node){form?.addEventListener('change',()=>form.requestSubmit());return;}
  const data=JSON.parse(node.textContent), target=document.querySelector('[data-rumble-image]'),
    button=document.querySelector('[data-rumble-download]'), status=document.querySelector('[data-rumble-status]');
  if(!data.rows.length){button.disabled=true;target.replaceChildren();status.textContent='No Readers match these settings.';form.addEventListener('change',()=>form.requestSubmit());return;}
  const W=1080,H=1350,PAD=56,GOLD='#e8cc90',INK='#f7f0df',MUTED='#b8b5c8';
  let url='',filename='',version=0,invalidated=false,opener=null;
  const dialog=document.createElement('dialog');dialog.className='rumble-image-dialog';dialog.setAttribute('aria-label','Enlarged Reader Rumble');
  const close=document.createElement('button');close.type='button';close.className='button secondary';close.textContent='Close preview';
  const viewport=document.createElement('div');viewport.className='rumble-image-viewport';viewport.tabIndex=0;viewport.setAttribute('aria-label','Full-resolution Reader Rumble');
  const large=document.createElement('img');large.alt='Full-resolution Reader Rumble';viewport.append(large);dialog.append(close,viewport);document.body.append(dialog);
  close.addEventListener('click',()=>dialog.close());dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close();});
  dialog.addEventListener('close',()=>opener?.focus());
  function clearPreview(){
    ++version;invalidated=true;button.disabled=true;target.replaceChildren();large.removeAttribute('src');
    if(dialog.open)dialog.close();if(url){URL.revokeObjectURL(url);url='';}
    status.textContent='Updating preview…';
  }
  // A new request obtains a newly authorized roster/ranking, including larger Top N.
  form.addEventListener('change',()=>{clearPreview();form.requestSubmit();});
  function loadImage(value){
    if(!value)return Promise.resolve(null);
    let parsed;try{parsed=new URL(value,location.href);}catch{return Promise.resolve(null);}
    if(!['http:','https:'].includes(parsed.protocol))return Promise.resolve(null);
    return new Promise(resolve=>{const image=new Image();image.crossOrigin='anonymous';image.referrerPolicy='no-referrer';
      const timer=setTimeout(()=>{image.src='';resolve(null);},5000);
      image.onload=()=>{clearTimeout(timer);resolve(image.naturalWidth&&image.naturalHeight?image:null);};
      image.onerror=()=>{clearTimeout(timer);resolve(null);};image.src=parsed.href;});
  }
  async function render(){
    const current=version;
    const [logo,banner,northbound]=await Promise.all([loadImage(data.logo),loadImage(data.banner),loadImage('/static/branding/northbound-header-dark.png')]);
    if(current!==version)return;
    const now=new Date(),parts=new Intl.DateTimeFormat('en-US',{timeZone:data.timezone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(now);
    const part=kind=>parts.find(p=>p.type===kind).value;
    filename=data.filename+'-'+part('year')+'-'+part('month')+'-'+part('day')+'.png';
    const timestamp=new Intl.DateTimeFormat('en-US',{timeZone:data.timezone,dateStyle:'medium',timeStyle:'long'}).format(now);
    const canvas=document.createElement('canvas');canvas.width=W;canvas.height=H;
    const ctx=canvas.getContext('2d');if(!ctx)throw Error('Canvas unavailable');
    function lines(value,size,width,family='Arial',weight='400'){
      ctx.font=`${weight} ${size}px ${family}`;const result=[];let line='';
      for(const word of String(value).split(/\s+/)){if(line&&ctx.measureText(line+' '+word).width>width){result.push(line);line='';}
        if(line)line+=' ';for(const char of word){if(line&&ctx.measureText(line+char).width>width){result.push(line);line='';}line+=char;}}
      if(line)result.push(line);return result;
    }
    function text(value,x,top,size,width,{color=INK,align='left',maxLines=1,minSize=size,family='Arial',weight='400'}={}){
      let rows=lines(value,size,width,family,weight);
      while(rows.length>maxLines&&size>minSize){size--;rows=lines(value,size,width,family,weight);}
      if(rows.length>maxLines){rows=rows.slice(0,maxLines);let last=rows[maxLines-1];ctx.font=`${weight} ${size}px ${family}`;
        while(last&&ctx.measureText(last+'…').width>width)last=Array.from(last).slice(0,-1).join('');rows[maxLines-1]=last+'…';}
      ctx.font=`${weight} ${size}px ${family}`;ctx.fillStyle=color;ctx.textAlign=align;
      rows.forEach((row,i)=>ctx.fillText(row,x,top+size+i*size*1.18));
    }
    function contain(image,x,top,width,height){const scale=Math.min(width/image.naturalWidth,height/image.naturalHeight);ctx.drawImage(image,x+(width-image.naturalWidth*scale)/2,top+(height-image.naturalHeight*scale)/2,image.naturalWidth*scale,image.naturalHeight*scale);}
    // Rounded panels, fine rules and medal accents stay evergreen; no themed assets.
    function panel(x,y,width,height,fill,stroke,radius=12){
      ctx.beginPath();ctx.roundRect(x,y,width,height,radius);ctx.fillStyle=fill;ctx.fill();
      if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=1;ctx.stroke();}
    }
    const metals=[GOLD,'#cfd7e2','#d7aa85'];
    function medal(rank,x,y,radius){
      const color=metals[rank-1];
      ctx.fillStyle=color;ctx.beginPath();ctx.moveTo(x-radius*.55,y+radius*.5);ctx.lineTo(x-radius*.7,y+radius*1.35);ctx.lineTo(x,y+radius*.95);ctx.lineTo(x+radius*.7,y+radius*1.35);ctx.lineTo(x+radius*.55,y+radius*.5);ctx.fill();
      const shine=ctx.createLinearGradient(x-radius,y-radius,x+radius,y+radius);shine.addColorStop(0,'#fff4dc');shine.addColorStop(.45,color);shine.addColorStop(1,'#72624c');
      ctx.beginPath();ctx.arc(x,y,radius,0,Math.PI*2);ctx.fillStyle=shine;ctx.fill();
      ctx.beginPath();ctx.arc(x,y,radius-5,0,Math.PI*2);ctx.strokeStyle='rgba(20,22,39,.45)';ctx.lineWidth=1.5;ctx.stroke();
      text(String(rank),x,y-radius*.67,radius*1.05,radius*1.5,{color:'#171b2c',align:'center',weight:'700'});
    }
    try{
      const gradient=ctx.createLinearGradient(0,0,W,H);gradient.addColorStop(0,'#111a2d');gradient.addColorStop(.55,'#17182c');gradient.addColorStop(1,'#251c30');ctx.fillStyle=gradient;ctx.fillRect(0,0,W,H);
      if(banner){
        // Cover the decorative header using the top of the authorized artwork.
        // Source cropping bounds the image; even portrait/light assets cannot spill below it.
        const region=520,scale=Math.max(W/banner.naturalWidth,region/banner.naturalHeight),sw=W/scale,sh=region/scale;
        ctx.drawImage(banner,(banner.naturalWidth-sw)/2,0,sw,sh,0,0,W,region);
        const veil=ctx.createLinearGradient(0,0,0,region);veil.addColorStop(0,'rgba(12,18,36,.76)');veil.addColorStop(.6,'rgba(12,18,36,.86)');veil.addColorStop(1,'rgba(23,24,44,1)');ctx.fillStyle=veil;ctx.fillRect(0,0,W,region);
      }
      ctx.beginPath();ctx.roundRect(28,28,W-56,H-56,24);ctx.strokeStyle='rgba(232,204,144,.42)';ctx.lineWidth=1.5;ctx.stroke();
      text('READER RUMBLE',PAD,54,62,W-2*PAD,{color:GOLD,family:'Georgia',weight:'700',minSize:52});
      ctx.fillStyle='rgba(232,204,144,.35)';ctx.fillRect(PAD,143,W-2*PAD,1);
      const identity=logo||northbound;
      if(identity){panel(PAD,163,190,164,'rgba(12,18,36,.65)','rgba(232,204,144,.3)',18);contain(identity,PAD+12,175,166,140);}
      const identityX=identity?270:PAD,identityWidth=W-PAD-identityX;
      text(data.team,identityX,171,38,identityWidth,{maxLines:2,minSize:30,weight:'600'});
      text(data.challenge,identityX,272,26,identityWidth,{color:MUTED,maxLines:2,minSize:22});
      panel(PAD,350,W-2*PAD,52,'rgba(12,18,36,.88)','rgba(232,204,144,.3)',10);
      text('Top '+data.top+' · '+data.metric,PAD+18,360,26,W-2*PAD-36,{color:GOLD,weight:'600'});
      const columns=data.top>=20?2:1,split=data.top===25?13:10,gap=40;
      const columnWidth=(W-2*PAD-gap*(columns-1))/columns;
      const counts=columns===1?[data.rows.length]:[Math.min(data.rows.length,split),Math.max(0,data.rows.length-split)];
      const rowHeight=Math.min(160,755/Math.max(1,...counts));
      const font=columns===2?(data.top===25?24:26):(data.top===10?30:28);
      const podium=data.top===5;
      const weights=[1.45,1.1,1.1,.85,.85],rowGap=12;
      const weightTotal=weights.slice(0,data.rows.length).reduce((a,b)=>a+b,0);
      let podiumY=430;
      for(let index=0;index<data.rows.length;index++){
        const row=data.rows[index],column=columns===2&&index>=split?1:0,position=column?index-split:index;
        const x=PAD+column*(columnWidth+gap),accent=metals[index]||INK;
        const height=podium?(755-rowGap*Math.max(0,data.rows.length-1))*weights[index]/weightTotal:rowHeight-6;
        const rowY=podium?podiumY:430+position*rowHeight;
        const featured=podium&&index<3;
        const fill=featured?(index===0?'rgba(86,66,39,.72)':'rgba(37,42,59,.96)'):'rgba(12,18,35,.94)';
        panel(x,rowY,columnWidth,height,fill,index<3?accent:'rgba(170,165,191,.16)',podium?16:8);
        if(!podium&&index<3){ctx.fillStyle=accent;ctx.fillRect(x,rowY+9,3,height-18);}
        const nameX=podium?x+106:x+(columns===2?52:66),numericWidth=columns===2?118:210;
        const valueX=x+columnWidth-(podium?22:14),nameWidth=valueX-numericWidth-14-nameX;
        const nameSize=podium?(index===0?36:index<3?32:30):font;
        const maxLines=Math.max(1,Math.min(3,Math.floor((height-(podium?20:8))/(nameSize*1.18))));
        const nameWeight=index<3?'600':'400';
        const actualLines=Math.min(maxLines,lines(row.username,nameSize,nameWidth,'Arial',nameWeight).length);
        const top=rowY+(height-actualLines*nameSize*1.18)/2;
        if(featured)medal(row.rank,x+49,rowY+height/2-(index===0?5:3),index===0?32:27);
        else text(String(row.rank),x+12,top,podium?28:font-2,podium?56:columns===2?32:48,{color:index<3?accent:MUTED,minSize:18,weight:index<3?'700':'400'});
        text(row.username,nameX,top,nameSize,nameWidth,{maxLines,weight:nameWeight});
        const value=Number(row.value).toLocaleString('en-US');let valueSize=podium?(index===0?44:index<3?36:32):font;
        const valueWeight=index<3?'700':'500';ctx.font=`${valueWeight} ${valueSize}px Arial`;
        while(ctx.measureText(value).width>numericWidth&&valueSize>10){valueSize--;ctx.font=`${valueWeight} ${valueSize}px Arial`;}
        text(value,valueX,rowY+(height-valueSize*1.18)/2,valueSize,numericWidth,{color:index<3?accent:GOLD,align:'right',weight:valueWeight});
        if(podium)podiumY+=height+rowGap;
      }
      ctx.fillStyle='#74603e';ctx.fillRect(PAD,1207,W-2*PAD,1);
      text('Generated '+timestamp+' · '+data.timezone,PAD,1220,21,W-2*PAD,{color:MUTED,maxLines:2,minSize:18});
      text('Current active roster · full Challenge credit',PAD,1290,18,665,{color:MUTED});
      if(northbound)contain(northbound,775,1275,250,42);else text('NORTHBOUND',775,1290,22,250,{color:GOLD});
      const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));if(!blob)throw Error('PNG unavailable');
      if(current!==version)return;
      url=URL.createObjectURL(blob);const preview=document.createElement('img');preview.src=url;preview.alt=`Reader Rumble · ${data.team} · ${data.metric} · ${data.rows.length} Readers`;preview.width=W;preview.height=H;
      const enlarge=document.createElement('button');enlarge.type='button';enlarge.className='rumble-png-button';enlarge.setAttribute('aria-label','Enlarge Reader Rumble');enlarge.setAttribute('aria-haspopup','dialog');enlarge.append(preview);target.replaceChildren(enlarge);large.src=url;
      enlarge.addEventListener('click',()=>{opener=enlarge;dialog.showModal();close.focus();});
      status.textContent='PNG ready. Select the image to enlarge it. Generated '+timestamp+' ('+data.timezone+').';button.disabled=false;
    }finally{canvas.width=canvas.height=0;}
  }
  button.addEventListener('click',()=>{if(!url||invalidated)return;const a=document.createElement('a');a.href=url;a.download=filename;a.click();});
  window.addEventListener('pagehide',event=>{if(url&&!event.persisted)URL.revokeObjectURL(url);});
  window.addEventListener('pageshow',event=>{if(event.persisted&&invalidated)form.requestSubmit();});
  render().catch(()=>{if(!invalidated)status.textContent='Could not create this PNG. The accessible leaderboard remains available.';});
})();
