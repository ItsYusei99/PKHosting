let SRV_CACHE = [];
async function loadServers() {
  const box = document.getElementById('srvList');
  if (!box) return;
  try {
    const d = await api('/api/servers');
    SRV_CACHE = (d && d.servers) || [];
    renderServers();
    const ps = document.getElementById('pwState');
    if (ps) ps.textContent = d.auth
      ? 'Protegido con contraseña. Para cambiarla escribe la actual y la nueva.'
      : 'Sin contraseña: cualquiera con acceso al panel puede entrar. Escribe solo la nueva para crearla.';
  } catch (e) {
    box.innerHTML = '<div class="file-row">Error al cargar servidores</div>';
  }
}
function renderServers() {
  const box = document.getElementById('srvList');
  if (!box) return;
  const cc = document.getElementById('srvCounts');
  const run = SRV_CACHE.filter(s => s.running).length;
  if (cc) cc.innerHTML = `<span class="dot on"></span>${run} en línea<span class="dot off" style="margin-left:8px"></span>${SRV_CACHE.length - run} apagados`;
  if (!SRV_CACHE.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin servidores</span></div>'; return; }
  box.innerHTML = SRV_CACHE.map(s => `
    <div class="mod-row${s.running ? '' : ' is-off'}">
      <span class="file-icon mod-ico" title="${s.running ? 'En línea' : 'Apagado'}">${SVG_SRV}</span>
      <span class="mod-main">
        <span class="mod-name" title="${escHtml(s.server_dir)}">${escHtml(s.name)}${s.current ? ' <span class="mchip ld">ACTUAL</span>' : ''}</span>
        <span class="mod-meta"><span>${escHtml(s.server_dir)}</span><span>MC :${s.mc_port}</span></span>
      </span>
      <span class="mod-state ${s.running ? 's-on' : 's-off'}">${s.running ? 'ON' : 'OFF'}</span>
      <span class="file-actions">
        ${s.current ? '' : `<button class="term-tool-btn" onclick="srvOpen(${jsArg(s.id)})">Abrir</button>`}
        <button class="icon-btn" title="Editar" onclick="srvShowForm(${jsArg(s.id)})">${SVG_PENCIL}</button>
        <button class="icon-btn danger" title="Eliminar" onclick="srvDelete(${jsArg(s.id)})">${SVG_TRASH}</button>
      </span>
    </div>`).join('');
}
function srvOpen(id) {
  location.href = '?server=' + encodeURIComponent(id) + location.hash;
}
function srvShowForm(id) {
  const f = document.getElementById('srvForm');
  const s = id ? SRV_CACHE.find(x => x.id === id) : null;
  document.getElementById('srvFormTitle').textContent = s ? `Editar '${s.name}'` : 'Añadir servidor';
  document.getElementById('srvEditId').value = s ? s.id : '';
  document.getElementById('srvName').value = s ? s.name : '';
  document.getElementById('srvDir').value = s ? s.server_dir : '';
  document.getElementById('srvMc').value = s ? s.mc_port : (SRV_CACHE.reduce((m, x) => Math.max(m, x.mc_port || 0), 25565) + 1);
  document.getElementById('srvRcon').value = s ? s.rcon_port : (SRV_CACHE.reduce((m, x) => Math.max(m, x.rcon_port || 0), 25575) + 1);
  document.getElementById('srvMem').value = s ? s.max_mem_gb : 4;
  f.style.display = 'block';
  f.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  if (!s) document.getElementById('srvName').focus();
}
function srvHideForm() {
  document.getElementById('srvForm').style.display = 'none';
  document.getElementById('srvEditId').value = '';
}
async function srvSubmit() {
  const id = document.getElementById('srvEditId').value;
  const body = {
    action: id ? 'update' : 'add',
    name: document.getElementById('srvName').value.trim(),
    server_dir: document.getElementById('srvDir').value.trim(),
    mc_port: parseInt(document.getElementById('srvMc').value, 10),
    rcon_port: parseInt(document.getElementById('srvRcon').value, 10),
    max_mem_gb: parseFloat(document.getElementById('srvMem').value)
  };
  if (id) body.id = id;
  try {
    const r = await api('/api/servers', body);
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
    if (r.ok !== false) srvHideForm();
  } catch (e) { showToast('Error de conexión', 'error'); }
  loadServers();
}
async function srvDelete(id) {
  const s = SRV_CACHE.find(x => x.id === id);
  if (!s) return;
  if (!await pkConfirm(`¿Quitar '${s.name}' del panel? No se borran sus archivos.`)) return;
  try {
    const r = await api('/api/servers', { action: 'delete', id });
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
    if (r.ok !== false) {
      if (s.current && r.switchTo) location.href = '?server=' + encodeURIComponent(r.switchTo) + location.hash;
      else loadServers();
    }
  } catch (e) { showToast('Error de conexión', 'error'); }
}
let SRV_BROWSE = '';
let SRV_BROWSE_DIRS = [];
function openSrvBrowser() {
  const cur = (document.getElementById('srvDir') || {}).value || '';
  document.getElementById('srvBrowseModal').style.display = 'flex';
  srvBrowseGo(cur.trim() || '~');
}
function srvBrowseClose() {
  document.getElementById('srvBrowseModal').style.display = 'none';
}
function srvBrowseCrumb(p) {
  const segs = String(p).split('/').filter(Boolean);
  let acc = '';
  let html = `<a onclick="srvBrowseGo('/');return false">/</a>`;
  segs.forEach(sg => {
    acc += '/' + sg;
    const target = acc;
    html += `<span style="color:var(--text-dim)"> / </span><a onclick="srvBrowseGoIdx(${jsArg(target)});return false">${escHtml(sg)}</a>`;
  });
  return html;
}
function srvBrowseGoIdx(path) { srvBrowseGo(path); }
async function srvBrowseGo(path) {
  const box = document.getElementById('srvBrowseList');
  try {
    const r = await (await fetch(U('/api/browse?path=' + encodeURIComponent(path)))).json();
    if (!r || r.ok === false) { showToast((r && r.msg) || 'No accesible', 'error'); return; }
    SRV_BROWSE = r.path;
    const base = r.path.endsWith('/') ? r.path.slice(0, -1) : r.path;
    SRV_BROWSE_DIRS = (r.dirs || []).map(d => base + '/' + d.name);
    document.getElementById('srvBrowsePath').innerHTML = srvBrowseCrumb(r.path);
    const dirs = r.dirs || [];
    box.innerHTML = dirs.length ? dirs.map((d, i) => `
      <div class="file-row" onclick="srvBrowseEnter(${i})" style="cursor:pointer">
        <span class="file-icon is-folder" title="Carpeta">${SVG_FOLDER}</span>
        <span class="file-name">${escHtml(d.name)}${d.ready ? ' <span class="mchip ld">server</span>' : ''}</span>
      </div>`).join('') : '<div class="file-row"><span class="file-name">Sin subcarpetas</span></div>';
  } catch (e) { showToast('Error de conexión', 'error'); }
}
function srvBrowseEnter(i) {
  const p = SRV_BROWSE_DIRS[i];
  if (p) srvBrowseGo(p);
}
function srvBrowseUp() {
  srvBrowseGo(SRV_BROWSE ? SRV_BROWSE + '/..' : '~');
}
function srvBrowsePick() {
  if (!SRV_BROWSE) return;
  document.getElementById('srvDir').value = SRV_BROWSE;
  srvBrowseClose();
  showToast('Carpeta seleccionada', 'success');
}

function durStep(id, delta, min, max) {
  const el = document.getElementById(id);
  el.dataset.dirty = 1;
  el.value = Math.max(min, Math.min(max, (parseInt(el.value) || 0) + delta));
}
// Sidebar por proximidad (estilo Rare UI)
document.querySelector('aside').addEventListener('mousemove', e => {
  document.querySelectorAll('.nav-item').forEach(el => {
    const r = el.getBoundingClientRect();
    const dx = e.clientX - (r.left + r.width / 2);
    const dy = e.clientY - (r.top + r.height / 2);
    const dist = Math.hypot(dx, dy);
    el.style.setProperty('--prox', Math.max(0, 1 - dist / 220).toFixed(2));
  });
});
document.querySelector('aside').addEventListener('mouseleave', () => {
  document.querySelectorAll('.nav-item').forEach(el => el.style.setProperty('--prox', 0));
});
// Progreso de scroll del contenido
document.querySelector('main').addEventListener('scroll', e => {
  const m = e.target;
  const p = m.scrollHeight - m.clientHeight > 0 ? (m.scrollTop / (m.scrollHeight - m.clientHeight)) * 100 : 0;
  document.getElementById('scrollProgress').style.width = p + '%';
});
