let fsPath = '';
let fsSort = { key: 'name', dir: 1, group: true };
function fsJoin(base, name) { return (base ? base + '/' : '') + name; }
function escHtml(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
// Argumento seguro para onclick="fn(...)": literal JS (JSON) y luego escape HTML del atributo
function jsArg(s) { return escHtml(JSON.stringify(String(s))); }
const SVG_FOLDER = '<svg class="ico" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
const SVG_FILE = '<svg class="ico" viewBox="0 0 24 24"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>';
const SVG_DL = '<svg class="ico" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
const SVG_PENCIL = '<svg class="ico" viewBox="0 0 24 24"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>';
const SVG_EDIT = '<svg class="ico" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
const SVG_TRASH = '<svg class="ico" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';
const SVG_BOX = '<svg class="ico" viewBox="0 0 24 24"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>';
const SVG_SRV = '<svg class="ico" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>';
function fileTone(name) {
  const e = (String(name).split('.').pop() || '').toLowerCase();
  if (e === 'json') return 'is-json';
  if (['properties', 'yml', 'yaml', 'toml', 'cfg', 'conf', 'ini'].includes(e)) return 'is-cfg';
  if (['log', 'gz', 'bak', 'old', 'tmp'].includes(e)) return 'is-log';
  if (e === 'jar') return 'is-jar';
  if (['zip', '7z', 'rar'].includes(e)) return 'is-zip';
  if (['txt', 'md', 'csv'].includes(e)) return 'is-txt';
  return 'is-file';
}
function fsSortKey(k) {
  fsSort.key = k;
  document.querySelectorAll('#fsSeg button').forEach(b => b.classList.toggle('on', b.dataset.k === k));
  loadFiles(false);
}
function fsToggleDir() { fsSort.dir *= -1; document.getElementById('fsSortDir').textContent = fsSort.dir === 1 ? '↓' : '↑'; loadFiles(false); }
function syncSw(id) {
  const el = document.getElementById(id);
  const sw = document.getElementById(id + 'Sw');
  if (sw && el) { sw.classList.toggle('on', el.checked); sw.setAttribute('aria-checked', String(el.checked)); }
}
function fsToggleSw(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.checked = !el.checked;
  syncSw(id);
  el.dispatchEvent(new Event('change'));
}
function swKey(id, ev) {
  if (ev.key === ' ' || ev.key === 'Enter') { ev.preventDefault(); fsToggleSw(id); }
}
function fsApplySort(files) {
  fsSort.group = document.getElementById('fsGroup').checked;
  const k = fsSort.key, d = fsSort.dir;
  return files.slice().sort((a, b) => {
    if (fsSort.group && a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let va, vb;
    if (k === 'mtime') { va = a.mtime || 0; vb = b.mtime || 0; }
    else if (k === 'size') { va = a.size_b || 0; vb = b.size_b || 0; }
    else { va = (a.name || '').toLowerCase(); vb = (b.name || '').toLowerCase(); }
    if (va < vb) return -1 * d;
    if (va > vb) return 1 * d;
    return 0;
  });
}
function fsRenderCrumb(cur) {
  const el = document.getElementById('fsBreadcrumb');
  const parts = (cur || '').split('/').filter(Boolean);
  let html = `<a data-p="" onclick="fsGoto(this.dataset.p);return false" href="#">/</a>`;
  let acc = '';
  for (const seg of parts) {
    acc = acc ? acc + '/' + seg : seg;
    html += ` <span style="color:var(--text-dim)">›</span> <a data-p="${escHtml(acc)}" onclick="fsGoto(this.dataset.p);return false" href="#">${escHtml(seg)}</a>`;
  }
  el.innerHTML = html;
}
function fsGoto(p) { fsNav(p, true); }
async function loadFiles(push = true) {
  const container = document.getElementById('filesContainer');
  try {
    const res = await fetch(U('/api/files?path=' + encodeURIComponent(fsPath)));
    const data = await res.json();
    const files = fsApplySort(Array.isArray(data) ? data : (data.entries || []));
    const cur = (data && typeof data === 'object' && data.path !== undefined) ? data.path : fsPath;
    fsRenderCrumb(cur);
    if (!files.length) { container.innerHTML = '<div class="file-row"><span class="file-name">Carpeta vacía</span></div>'; }
    else {
      container.innerHTML = files.map(f => {
        const full = escHtml(fsJoin(fsPath, f.name));
        const link = f.is_dir
          ? `<a href="#" data-path="${full}" onclick="fsEnter(this.dataset.path);return false" style="color:var(--info)">${escHtml(f.name)}/</a>`
          : escHtml(f.name);
        const editable = !f.is_dir && (f.size_b || 0) <= 2 * 1024 * 1024;
        const dt = f.mtime ? new Date(f.mtime * 1000).toLocaleString() : '';
        return `<div class="file-row">
          <span class="file-icon ${f.is_dir ? 'is-folder' : fileTone(f.name)}" title="${f.is_dir ? 'Carpeta' : 'Archivo'}">${f.is_dir ? SVG_FOLDER : SVG_FILE}</span>
          <span class="file-name">${link}</span>
          <span class="file-size" title="${escHtml(dt)}">${escHtml(f.size || '')}</span>
          <span class="file-actions">
            ${f.is_dir ? '' : `<button class="icon-btn" title="Descargar" data-path="${full}" onclick="fsDownload(this.dataset.path)">${SVG_DL}</button>`}
            ${editable ? `<button class="icon-btn" title="Editar" data-path="${full}" onclick="fsEdit(this.dataset.path)">${SVG_EDIT}</button>` : ''}
            <button class="icon-btn" title="Renombrar" data-path="${full}" onclick="fsRename(this.dataset.path)">${SVG_PENCIL}</button>
            <button class="icon-btn danger" title="Eliminar" data-path="${full}" onclick="fsDelete(this.dataset.path)">${SVG_TRASH}</button>
          </span>
        </div>`;
      }).join('');
    }
  } catch (e) {
    container.innerHTML = '<div class="file-row">Error al cargar archivos</div>';
  }
  if (push) { try { history.pushState({ fsPath }, '', '#archivos/' + fsPath); } catch (err) {} }
}
function fsNav(p, push = true) { fsPath = p; try { localStorage.setItem('pkFs', fsPath); } catch (e) {} loadFiles(push); }
function fsEnter(p) { fsNav(p, true); }
function fsNavUp() { fsNav(fsPath.split('/').slice(0, -1).join('/'), true); }
window.addEventListener('popstate', (e) => {
  const p = (e.state && e.state.fsPath !== undefined) ? e.state.fsPath : '';
  fsPath = p;
  loadFiles(false);
});
try { history.replaceState({ fsPath: '' }, '', location.pathname + location.hash); } catch (err) {}
let fsEditOrig = null;   // contenido al abrir/guardar: detecta cambios sin guardar
async function fsEdit(p) {
  document.getElementById('fsEditPath').textContent = p;
  document.getElementById('fsEditor').value = 'Cargando...';
  fsEditOrig = null;
  document.getElementById('fsEditModal').style.display = 'flex';
  const r = await fsPost('read', { path: p });
  const ed = document.getElementById('fsEditor');
  ed.value = r.ok ? (r.content || '') : ('Error: ' + (r.msg || 'no se pudo leer'));
  fsEditOrig = r.ok ? ed.value : null;
  fsUpdateEditorStats();
}
async function fsCloseEdit(force) {
  const ed = document.getElementById('fsEditor');
  if (!force && fsEditOrig !== null && ed.value !== fsEditOrig &&
      !await pkConfirm('Hay cambios sin guardar. ¿Descartarlos?', { danger: true, okText: 'Descartar' })) return;
  document.getElementById('fsEditModal').style.display = 'none';
  fsEditOrig = null;
}
async function fsSaveEdit() {
  const p = document.getElementById('fsEditPath').textContent;
  const btn = document.getElementById('fsSaveBtn');
  if (btn) btn.disabled = true;
  try {
    const r = await fsPost('write', { path: p, content: document.getElementById('fsEditor').value });
    showToast(r.msg || 'OK', r.ok === false ? 'error' : undefined);
    if (r.ok) { fsCloseEdit(true); loadFiles(false); }
  } catch (e) { showToast('No se pudo guardar', 'error'); }
  finally { if (btn) btn.disabled = false; }
}
function fsDownloadCurrent() {
  const p = document.getElementById('fsEditPath').textContent;
  if (p && p !== '—') fsDownload(p);
}
async function fsPost(action, extra) {
  const res = await fetch(U('/api/fs'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, ...extra }) });
  return res.json();
}
async function fsCreate(kind) {
  const v = (await pkPrompt(kind === 'dir' ? 'Nombre de la nueva carpeta:' : 'Nombre del nuevo archivo:') || '').trim();
  if (!v) return;
  const clean = v.replace(new RegExp('^/+'), '');
  const r = await fsPost(kind === 'dir' ? 'mkdir' : 'touch', { path: fsJoin(fsPath, clean) });
  showToast(r.msg || 'OK'); loadFiles();
}
async function fsUpload() {
  const inp = document.getElementById('fsUploadInput');
  if (!inp.files.length) return;
  const fd = new FormData();
  for (const f of inp.files) fd.append('files', f, f.name);
  const uz = document.getElementById('fsUnzip') && document.getElementById('fsUnzip').checked ? '&unzip=1' : '';
  const bar = document.getElementById('fsUpProg');
  if (bar) { bar.value = 0; bar.hidden = false; }
  try {
    const r = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', U('/api/upload?path=' + encodeURIComponent(fsPath) + uz));
      xhr.upload.onprogress = ev => { if (bar && ev.lengthComputable) bar.value = Math.round(ev.loaded * 100 / ev.total); };
      xhr.onload = () => {
        if (xhr.status === 401) { location.reload(); return; }
        try { resolve(JSON.parse(xhr.responseText)); } catch (e) { reject(new Error('respuesta inválida (' + xhr.status + ')')); }
      };
      xhr.onerror = () => reject(new Error('sin conexión'));
      xhr.send(fd);
    });
    showToast(r.msg || 'OK', r.ok === false ? 'error' : undefined);
  } catch (e) { showToast('Error al subir: ' + e.message, 'error'); }
  if (bar) bar.hidden = true;
  inp.value = '';
  loadFiles();
}
function fsDownload(p) { window.open(U('/api/download?path=' + encodeURIComponent(p)), '_blank'); }
async function fsRename(p) {
  const dest = (await pkPrompt('Renombrar / mover a (ruta relativa):', p) || '').trim();
  if (!dest || dest === p) return;
  const r = await fsPost('rename', { path: p, dest });
  showToast(r.msg || 'OK'); loadFiles();
}
async function fsDelete(p) {
  if (!await pkConfirm('¿Eliminar "' + p + '"?')) return;
  const r = await fsPost('delete', { path: p });
  showToast(r.msg || 'OK'); loadFiles();
}
