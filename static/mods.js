let MODS_CACHE = [];
let MODS_VIEW = [];
let MOD_UPDATES = {};
async function loadMods() {
  try {
    const d = await api('/api/mods');
    MODS_CACHE = (d && d.mods) || [];
    renderMods();
    modCheckUpdates(false);
  } catch (e) {
    document.getElementById('modsList').innerHTML = '<div class="file-row">Error al cargar mods</div>';
  }
}
function modIsVerTok(t) {
  if (!t) return false;
  let hasDigit = false;
  for (const ch of t) {
    if (ch >= '0' && ch <= '9') { hasDigit = true; continue; }
    if (ch === '.' || ch === 'v' || ch === 'V') continue;
    return false;
  }
  return hasDigit;
}
function modIsLoaderWord(t) {
  const w = String(t).toLowerCase();
  return w === 'neoforge' || w === 'neo' || w === 'forge' || w === 'fabric' || w === 'quilt';
}
function modLoaderName(t) {
  const w = String(t).toLowerCase();
  if (w === 'neoforge' || w === 'neo') return 'NeoForge';
  if (w === 'forge') return 'Forge';
  if (w === 'fabric') return 'Fabric';
  if (w === 'quilt') return 'Quilt';
  return t;
}
function modIsWord(t) {
  if (!t) return false;
  for (const ch of t) {
    if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z'))) return false;
  }
  return true;
}
function modPretty(name) {
  let base = String(name);
  if (base.toLowerCase().endsWith('.jar')) base = base.slice(0, -4);
  let loader = null;
  if (base.charAt(0) === '[') {
    const j = base.indexOf(']');
    if (j > 1) { loader = base.slice(1, j).trim(); base = base.slice(j + 1).trim(); }
  }
  const toks = base.split(/[ _-]+/);
  if (!loader) {
    for (const t of toks) {
      if (modIsLoaderWord(t)) { loader = modLoaderName(t); break; }
    }
  }
  let ver = null;
  const vt = toks.slice();
  if (vt.length && modIsLoaderWord(vt[vt.length - 1])) vt.pop();
  if (vt.length) {
    const last = vt[vt.length - 1];
    if (modIsVerTok(last)) ver = last;
    else if (modIsWord(last) && vt.length > 1 && modIsVerTok(vt[vt.length - 2]))
      ver = vt[vt.length - 2] + ' ' + last;
  }
  return { loader, ver };
}
function renderMods() {
  const box = document.getElementById('modsList');
  const q = ((document.getElementById('modSearch') || {}).value || '').toLowerCase().trim();
  const on = MODS_CACHE.filter(m => m.enabled).length;
  const cc = document.getElementById('modCounts');
  if (cc) cc.innerHTML = `<span class="dot on"></span>${on} activos<span class="dot off" style="margin-left:8px"></span>${MODS_CACHE.length - on} apagados`;
  if (!MODS_CACHE.length) { MODS_VIEW = []; box.innerHTML = '<div class="file-row"><span class="file-name">Sin mods</span></div>'; return; }
  MODS_VIEW = MODS_CACHE.filter(m => !q || m.name.toLowerCase().includes(q));
  if (!MODS_VIEW.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin resultados para esa búsqueda</span></div>'; return; }
  const mb = m => m > 1048576 ? (m / 1048576).toFixed(1) + ' MB' : Math.round(m / 1024) + ' KB';
  box.innerHTML = MODS_VIEW.map((m, i) => {
    const p = modPretty(m.name);
    const chips = (p.loader ? `<span class="mchip ld">${escHtml(p.loader)}</span>` : '')
      + (p.ver ? `<span class="mchip ver">v${escHtml(p.ver.replace(/^v/i, ''))}</span>` : '');
    return `<div class="mod-row${m.enabled ? '' : ' is-off'}">
      <span class="file-icon mod-ico" title="${m.enabled ? 'Activo' : 'Desactivado'}">${SVG_BOX}</span>
      <span class="mod-main">
        <span class="mod-name" title="${escHtml(m.name)}">${escHtml(m.name)}</span>
        <span class="mod-meta">${chips}<span>${mb(m.size)}</span></span>
      </span>
      <span class="mod-state ${m.enabled ? 's-on' : 's-off'}">${m.enabled ? 'ON' : 'OFF'}</span>
      ${MOD_UPDATES[m.name] ? `<button class="icon-btn" title="Actualizar a ${escHtml(MOD_UPDATES[m.name].latest)} (${escHtml(MOD_UPDATES[m.name].date)})" onclick="modUpdateIdx(${i})" style="color:#4ade80">\u2191</button>` : ''}
      ${m.prev ? `<button class="icon-btn" title="Revertir a la copia anterior" onclick="modRollbackIdx(${i})" style="color:#facc15">\u21a9</button>` : ''}
      <button class="msw" role="switch" aria-checked="${m.enabled}" aria-label="Activar o desactivar ${escHtml(m.name)}" title="${m.enabled ? 'Desactivar' : 'Activar'} (requiere reinicio)" onclick="modToggleIdx(${i})"></button>
      <button class="icon-btn danger" title="Eliminar" onclick="modDelIdx(${i})">${SVG_TRASH}</button>
    </div>`;
  }).join('');
}
async function modToggleIdx(i) {
  const m = MODS_VIEW[i];
  if (!m) return;
  const c = MODS_CACHE.find(x => x.name === m.name);
  if (!c) return;
  c.enabled = !c.enabled;
  renderMods();
  try {
    const r = await api('/api/mod', { action: 'toggle', name: m.name });
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
    if (r.ok === false) { c.enabled = !c.enabled; renderMods(); }
  } catch (e) { c.enabled = !c.enabled; renderMods(); showToast('Error de conexión', 'error'); }
}
async function modDelIdx(i) {
  const m = MODS_VIEW[i];
  if (!m) return;
  if (!await pkConfirm(`¿Eliminar el mod "${m.name}"?`)) return;
  try {
    const r = await api('/api/mod', { action: 'delete', name: m.name });
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modToggle(name) { // compatibilidad
  try {
    const r = await api('/api/mod', { action: 'toggle', name });
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modCheckUpdates(force) {
  const bar = document.getElementById('modUpdBar');
  try {
    const d = await (await fetch(U('/api/mod-updates' + (force ? '?refresh=1' : '')))).json();
    if (!d.ok) return;
    MOD_UPDATES = {};
    (d.available || []).forEach(a => { MOD_UPDATES[a.file] = a; });
    const n = (d.available || []).length;
    if (d.running) {
      if (bar) bar.textContent = 'Comprobando actualizaciones en Modrinth (' + d.checked + ' revisados)...';
      setTimeout(() => modCheckUpdates(false), 15000);
    } else if (bar) {
      bar.textContent = n ? n + ' actualización(es) disponible(s) — servidor detenido requerido' +
        (d.unknown ? ' · ' + d.unknown + ' sin coincidencia en Modrinth' : '') : 'Todo al día' +
        (d.unknown ? ' · ' + d.unknown + ' sin coincidencia en Modrinth' : '');
    }
    renderMods();
  } catch (e) {}
}
async function modUpdateIdx(i) {
  const m = MODS_VIEW[i];
  if (!m) return;
  const u = MOD_UPDATES[m.name];
  if (!u) return;
  if (!await pkConfirm('¿Actualizar "' + m.name + '" a ' + u.latest + ' (' + u.date + ')?\nEl servidor debe estar detenido. Se guarda copia anterior.')) return;
  try {
    const r = await api('/api/mod', { action: 'update', name: m.name });
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modRollbackIdx(i) {
  const m = MODS_VIEW[i];
  if (!m) return;
  if (!await pkConfirm('¿Revertir "' + m.name + '" a la copia anterior? El servidor debe estar detenido.')) return;
  try {
    const r = await api('/api/mod', { action: 'rollback', name: m.name });
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modDel(name) { // compatibilidad
  if (!await pkConfirm(`¿Eliminar el mod "${name}"?`)) return;
  try {
    const r = await api('/api/mod', { action: 'delete', name });
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modBulk(enable) {
  const n = MODS_CACHE.filter(m => m.enabled !== enable).length;
  if (!n) { showToast('Nada que cambiar', 'info'); return; }
  if (!await pkConfirm(`${enable ? 'Activar' : 'Desactivar'} ${n} mod(s)? Requiere reinicio del servidor.`)) return;
  try {
    const r = await api('/api/mod', { action: enable ? 'enable_all' : 'disable_all' });
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
