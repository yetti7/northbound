const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const script=fs.readFileSync('static/js/branding_form.js','utf8');
for (const hasStored of [false,true]) {
 const fields={},images={},labels={};
 for(const kind of ['logo','banner']) {
  fields[kind]={files:[],listeners:{},value:'',addEventListener(n,f){this.listeners[n]=f;}};
  if(hasStored)fields[kind+'-clear']={checked:false,listeners:{},addEventListener(n,f){this.listeners[n]=f;}};
  images[kind]={src:hasStored?'stored.png':'inherited.png',hidden:false,dataset:{fallback:'inherited.png',fallbackLabel:'Inherited from Challenge',sourceLabel:hasStored?'Stored Team artwork':'Inherited from Challenge'},getAttribute(n){return this[n];},removeAttribute(n){delete this[n];}};
  labels[kind]={textContent:''};
 }
 const form={hasAttribute(){return false;},querySelector(q){if(q==='[name="branding_mode"]')return null;return fields[q.match(/name="([^"]+)/)[1]];}};
 const document={querySelector(q){if(q==='[data-branding-form]')return form;if(q==='[data-branding-header-preview]')return null;const kind=q.match(/="([^"]+)/)[1];return q.includes('preview')?images[kind]:labels[kind];}};
 vm.runInNewContext(script,{document,URL:{createObjectURL(){return 'blob:replacement';},revokeObjectURL(){}}});
 assert.equal(images.logo.src,hasStored?'stored.png':'inherited.png');
 fields.logo.files=[{type:'image/png'}];fields.logo.listeners.change();
 assert.equal(images.logo.src,'blob:replacement');assert.equal(labels.logo.textContent,'Selected replacement — not saved');
 if(hasStored){fields['logo-clear'].checked=true;fields['logo-clear'].listeners.change();assert.equal(images.logo.src,'inherited.png');assert.equal(labels.logo.textContent,'Inherited from Challenge');fields['logo-clear'].checked=false;fields.logo.files=[];fields['logo-clear'].listeners.change();assert.equal(images.logo.src,'stored.png');}
}
console.log('Branding previews: inherited, replacement, removal fallback and restoration passed.');

// The same raw asset cards feed all three unsaved child display modes.
{
 const toggle=()=>({values:{},toggle(k,v){this.values[k]=v;}});
 const images=Object.fromEntries(['logo','banner'].map(k=>[k,{src:k+'.png',hidden:false,dataset:{sourceLabel:'Stored Team artwork'},getAttribute(k){return this[k]},removeAttribute(k){delete this[k]}}]));
 const headerImages=Object.fromEntries(['logo','banner'].map(k=>[k,{hidden:false,removeAttribute(k){delete this[k]}}]));
 const mode={value:'banner',addEventListener(n,f){this[n]=f}};
 const fields={branding_mode:mode};
 for(const k of ['logo','banner'])fields[k]={files:[],addEventListener(){}};
 const header={classList:toggle(),querySelector(q){return headerImages[q.endsWith('logo')?'logo':'banner']}};
 const container={dataset:{legacyVisibility:'false'},querySelector(){return header}};
 const form={hasAttribute(){return false},addEventListener(){},querySelector(q){return fields[q.match(/name="([^"]+)/)[1]]}};
 const document={querySelector(q){if(q==='[data-branding-form]')return form;if(q==='[data-branding-header-preview]')return container;if(q.includes('data-branding-source'))return {};return images[q.match(/="([^"]+)/)[1]]}};
 vm.runInNewContext(script,{document,URL:{createObjectURL(){},revokeObjectURL(){}}});
 for(const value of ['logo','banner_only','banner']){mode.value=value;mode.change();assert.equal(headerImages.logo.hidden,value==='banner_only');assert.equal(headerImages.banner.hidden,value==='logo');assert.equal(header.classList.values['is-artwork-showcase'],value==='banner_only')}
 console.log('Independent unsaved header modes and showcase preview passed.');
}
