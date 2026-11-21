(function(){
  const T=window.TOKEN, send=document.getElementById('send'), hint=document.getElementById('hint');
  const OK='<svg class="i st" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg>',
        BAD='<svg class="i st" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5M12 16v.01"/></svg>',
        WAIT='<svg class="i st" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
        X='<svg class="i" viewBox="0 0 24 24" style="width:18px;height:18px"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  const state={}; // slot -> [{id,name,ok,msg,pending}]
  (window.EXISTING||[]).forEach(f=>{(state[f.slot]=state[f.slot]||[]).push({id:f.file_id,name:f.name,ok:f.ok,msg:f.msg});});
  const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const friendly=r=>{ if(!r) return 'No se pudo recibir este archivo. Intenta de nuevo.';
    return (r.message||'No se pudo recibir este archivo.')+(r.hint?' '+r.hint:''); };
  function render(slot){
    const box=document.getElementById('files-'+slot), items=state[slot]||[];
    box.innerHTML=items.map((f,i)=>`<div class="file ${f.pending?'':(f.ok?'ok':'bad')}">${f.pending?WAIT:(f.ok?OK:BAD)}<div class="nm">${esc(f.name)}${f.msg?`<span class="why">${esc(f.msg)}</span>`:''}</div>
      ${f.pending?'':`<button type="button" class="x" data-slot="${slot}" data-i="${i}" aria-label="Quitar">${X}</button>`}</div>`).join('');
    document.querySelector(`[data-slot="${slot}"].slot`).classList.toggle('ok',items.some(f=>f.ok));
    const busy=Object.values(state).some(a=>a.some(f=>f.pending));
    const missing=[...document.querySelectorAll('.slot')].filter(s=>!(state[s.dataset.slot]||[]).some(f=>f.ok)).map(s=>s.querySelector('h3').firstChild.textContent.trim());
    send.disabled=busy||missing.length>0;
    hint.textContent=busy?'Subiendo…':(missing.length?'Falta: '+missing.join(', ')+'.':'Todo listo para enviar.');
  }
  Object.keys(state).forEach(render);
  document.querySelectorAll('[data-pick]').forEach(b=>b.addEventListener('click',()=>document.getElementById('in-'+b.dataset.pick).click()));
  document.querySelectorAll('input[type=file]').forEach(inp=>inp.addEventListener('change',()=>{
    const slot=inp.id.slice(3); if(!inp.files.length) return;
    const names=[...inp.files].map(f=>f.name); state[slot]=(state[slot]||[]).concat(names.map(n=>({name:n,pending:true})));
    render(slot);
    const fd=new FormData(); fd.append('slot',slot); [...inp.files].forEach(f=>fd.append('files',f)); inp.value='';
    fetch(`/r/${T}/archivos`,{method:'POST',body:fd}).then(async r=>{
      const j=await r.json().catch(()=>({})); state[slot]=state[slot].filter(f=>!f.pending);
      if(r.ok){ j.results.forEach(x=>state[slot].push({id:x.file_id,name:x.name,ok:x.status==='formato_valido',msg:x.status==='formato_valido'?'':friendly(x.rejection)})); }
      else names.forEach(n=>state[slot].push({name:n,ok:false,msg:j.detail||'No se pudo enviar el archivo. Intenta de nuevo.'}));
      render(slot);
    }).catch(()=>{ state[slot]=state[slot].filter(f=>!f.pending); names.forEach(n=>state[slot].push({name:n,ok:false,msg:'Se perdió la conexión. Intenta de nuevo.'})); render(slot); });
  }));
  document.addEventListener('click',async e=>{
    const b=e.target.closest('.x'); if(!b) return; const slot=b.dataset.slot, i=+b.dataset.i, f=state[slot][i];
    if(f.id) await fetch(`/r/${T}/quitar/${f.id}`,{method:'POST'}); state[slot].splice(i,1); render(slot); });
  send.addEventListener('click',async()=>{
    send.disabled=true; send.textContent='Enviando…';
    const r=await fetch(`/r/${T}/enviar`,{method:'POST'}); const j=await r.json().catch(()=>({}));
    if(r.ok) location.href=`/r/${T}/gracias`; else { hint.textContent=j.error||'No se pudo enviar. Intenta de nuevo.'; send.disabled=false; send.textContent=window.RESEND?'Enviar de nuevo':'Enviar documentos'; }
  });
})();
