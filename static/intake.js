(function(){
  const T=window.TOKEN, list=document.getElementById('list'), input=document.getElementById('files'), send=document.getElementById('send'),
        pt=document.getElementById('pt'), bar=document.getElementById('bar'), fill=document.getElementById('barfill'), state=document.getElementById('state'),
        cancel=document.getElementById('cancel'), hint=document.getElementById('hint');
  let rows=[], xhr=null;
  const LABEL={formato_valido:['Aceptado','b-ok'],duplicado:['Duplicado','b-warn'],formato_invalido:['Rechazado','b-err']};
  const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  function render(){
    if(!rows.length){list.innerHTML='Aún no ha cargado archivos.';}
    else list.innerHTML='<table><tr><th>Archivo</th><th>Estado</th><th>Detalle</th><th></th></tr>'+rows.map(r=>{
      const l=LABEL[r.status]||[r.status,'b-pend'];
      const det=r.rejection?`<b>${esc(r.rejection.message)}</b><br><span class="muted">Qué hacer: ${esc(r.rejection.hint)}</span>`:(r.warnings||[]).map(esc).join('<br>')||`${r.pages||1} página(s)`;
      const origin=r.source&&r.source.startsWith('zip:')?`<div class="muted">dentro de ${esc(r.source.slice(4))}</div>`:'';
      return `<tr><td>${esc(r.name)}${origin}</td><td><span class="badge ${l[1]}">${l[0]}</span></td><td>${det}</td><td><button type="button" class="sec" data-id="${r.file_id}">Quitar</button></td></tr>`;}).join('')+'</table>';
    const ok=rows.some(r=>r.status==='formato_valido'); send.disabled=!(ok&&pt.value);
    hint.textContent=ok?(pt.value?'Listo para enviar.':'Elija el tipo de proyecto.'):'Cargue al menos un archivo válido y elija el tipo de proyecto.';
  }
  list.addEventListener('click',async e=>{const id=e.target.dataset&&e.target.dataset.id; if(!id)return;
    await fetch(`/r/${T}/quitar/${id}`,{method:'POST'}); rows=rows.filter(r=>r.file_id!==id); render();});
  pt.addEventListener('change',render);
  input.addEventListener('change',()=>{
    if(!input.files.length)return; const fd=new FormData(); for(const f of input.files)fd.append('files',f);
    xhr=new XMLHttpRequest(); xhr.open('POST',`/r/${T}/archivos`);
    bar.style.display='block'; cancel.style.display='inline-block'; fill.style.width='0'; state.textContent='Subiendo y validando archivos…';
    xhr.upload.onprogress=e=>{if(e.lengthComputable)fill.style.width=(100*e.loaded/e.total)+'%';};
    xhr.onload=()=>{cancel.style.display='none';bar.style.display='none';
      if(xhr.status===200){const res=JSON.parse(xhr.responseText).results; rows=rows.concat(res);
        const bad=res.filter(r=>r.status!=='formato_valido').length; state.textContent=`${res.length-bad} aceptado(s), ${bad} con observaciones.`;}
      else{let m='No se pudo cargar.';try{m=JSON.parse(xhr.responseText).detail||m}catch(_){} state.textContent=m;}
      input.value='';render();};
    xhr.onerror=()=>{cancel.style.display='none';state.textContent='Error de conexión. Intente de nuevo.';};
    xhr.onabort=()=>{cancel.style.display='none';bar.style.display='none';state.textContent='Carga cancelada.';};
    xhr.send(fd);});
  cancel.addEventListener('click',()=>xhr&&xhr.abort());
  render();
})();
