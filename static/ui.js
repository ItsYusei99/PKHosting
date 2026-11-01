/* ══════════════════════════════════════════════════
   GLOBAL COMMAND PALETTE (Ctrl + K)
══════════════════════════════════════════════════ */
const PALETTE_ACTIONS = [
  { title: 'Inicio', cat: 'Navegar', action: () => switchTab('home') },
  { title: 'Consola del servidor', cat: 'Navegar', action: () => switchTab('console') },
  { title: 'Jugadores y moderación', cat: 'Navegar', action: () => switchTab('players') },
  { title: 'Métricas del sistema & TPS', cat: 'Navegar', action: () => switchTab('metrics') },
  { title: 'Archivos del servidor', cat: 'Navegar', action: () => switchTab('files') },
  { title: 'Mods', cat: 'Navegar', action: () => switchTab('mods') },
  { title: 'Mundos y MCA Region Cleaner', cat: 'Navegar', action: () => switchTab('worlds') },
  { title: 'Copias de seguridad (Backups)', cat: 'Navegar', action: () => switchTab('backups') },
  { title: 'Tareas programadas', cat: 'Navegar', action: () => switchTab('tasks') },
  { title: 'Historial de conexiones', cat: 'Navegar', action: () => switchTab('history') },
  { title: 'Flota de servidores', cat: 'Navegar', action: () => switchTab('fleet') },
  { title: 'Diagnóstico (crashes, JVM, logs)', cat: 'Navegar', action: () => switchTab('diag') },
  { title: 'Configuración y Ajustes', cat: 'Navegar', action: () => switchTab('settings') },
  { title: 'Iniciar Servidor', cat: 'Control', action: () => serverAction('start') },
  { title: 'Reiniciar Servidor', cat: 'Control', action: () => serverAction('restart') },
  { title: 'Detener Servidor', cat: 'Control', action: () => serverAction('stop') },
  { title: 'Reload Servidor (Datapacks/Mods)', cat: 'Control', action: () => serverAction('reload') },
  { title: 'Hacer Backup ahora', cat: 'Backups', action: () => { switchTab('backups'); setTimeout(backupNow, 300); } },
  { title: 'Diagnóstico de Salud Spark', cat: 'Rendimiento', action: () => sparkQuickAction('health') },
  { title: 'Explorador de Mods Modrinth', cat: 'Mods', action: () => { switchTab('mods'); setTimeout(toggleModrinthView, 300); } },
  { title: 'Pantalla completa de terminal', cat: 'Herramientas', action: () => toggleTermFullscreen() },
  { title: 'Copiar IP pública', cat: 'Herramientas', action: () => copyIp() },
  { title: 'Cerrar sesión', cat: 'Sesión', action: () => logout() }
];

let PALETTE_ACTIVE_IDX = 0;
let PALETTE_FILTERED = [];

function openCmdPalette() {
  document.getElementById('cmdPalette').style.display = 'flex';
  const inp = document.getElementById('cpInput');
  inp.value = '';
  renderCmdPalette();
  inp.focus();
}
function closeCmdPalette() {
  document.getElementById('cmdPalette').style.display = 'none';
}
function renderCmdPalette() {
  const q = document.getElementById('cpInput').value.toLowerCase().trim();
  PALETTE_FILTERED = PALETTE_ACTIONS.filter(a => !q || a.title.toLowerCase().includes(q) || a.cat.toLowerCase().includes(q));
  PALETTE_ACTIVE_IDX = 0;
  const list = document.getElementById('cpList');
  if (!PALETTE_FILTERED.length) {
    list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-dim);font-size:13px">Sin coincidencias</div>';
    return;
  }
  list.innerHTML = PALETTE_FILTERED.map((item, idx) => `
    <div class="cp-item${idx === PALETTE_ACTIVE_IDX ? ' active' : ''}" onclick="runPaletteItem(${idx})">
      <span>${escHtml(item.title)}</span>
      <span class="cp-badge">${escHtml(item.cat)}</span>
    </div>`).join('');
}
function handlePaletteKey(e) {
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (PALETTE_FILTERED.length) {
      PALETTE_ACTIVE_IDX = (PALETTE_ACTIVE_IDX + 1) % PALETTE_FILTERED.length;
      updatePaletteSelection();
    }
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (PALETTE_FILTERED.length) {
      PALETTE_ACTIVE_IDX = (PALETTE_ACTIVE_IDX - 1 + PALETTE_FILTERED.length) % PALETTE_FILTERED.length;
      updatePaletteSelection();
    }
  } else if (e.key === 'Enter') {
    e.preventDefault();
    runPaletteItem(PALETTE_ACTIVE_IDX);
  } else if (e.key === 'Escape') {
    closeCmdPalette();
  }
}
function updatePaletteSelection() {
  document.querySelectorAll('.cp-item').forEach((el, idx) => {
    el.classList.toggle('active', idx === PALETTE_ACTIVE_IDX);
    if (idx === PALETTE_ACTIVE_IDX) el.scrollIntoView({ block: 'nearest' });
  });
}
function runPaletteItem(idx) {
  const item = PALETTE_FILTERED[idx];
  if (item && item.action) {
    closeCmdPalette();
    item.action();
  }
}
window.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    const p = document.getElementById('cmdPalette');
    if (p.style.display === 'flex') closeCmdPalette();
    else openCmdPalette();
  } else if (e.key === 'Escape') {
    closeCmdPalette();
    closePlayerModal();
    if (document.querySelector('.terminal-wrapper.fullscreen')) toggleTermFullscreen();
    const mc = document.getElementById('mcaMapCard');
    if (mc && mc.classList.contains('map-full')) toggleMapFullscreen();
  }
});

/* ══════════════════════════════════════════════════
   TERMINAL FULLSCREEN & THEMES & EDITOR UPGRADES
══════════════════════════════════════════════════ */
function toggleTermFullscreen() {
  const w = document.querySelector('.terminal-wrapper');
  w.classList.toggle('fullscreen');
}
function setTermTheme(th) {
  const w = document.querySelector('.terminal-wrapper');
  ['matrix', 'monokai', 'tokyo', 'dracula'].forEach(t => w.classList.remove('term-theme-' + t));
  if (th !== 'default') {
    w.classList.add('term-theme-' + th);
  }
  try { localStorage.setItem('pkTermTheme', th); } catch(e) {}
}
(function restoreTermTheme() {
  try {
    const saved = localStorage.getItem('pkTermTheme');
    if (saved) {
      const sel = document.getElementById('termThemeSel');
      if (sel) sel.value = saved;
      setTermTheme(saved);
    }
  } catch(e) {}
})();

function fsUpdateEditorStats() {
  const ed = document.getElementById('fsEditor');
  const lines = ed.value.split(String.fromCharCode(10)).length;
  const chars = ed.value.length;
  document.getElementById('fsEditStats').textContent = `${lines} líneas · ${chars} caracteres`;
}
function fsHandleEditorTab(e) {
  if (e.key === 'Tab') {
    e.preventDefault();
    const el = e.target;
    const start = el.selectionStart;
    const end = el.selectionEnd;
    el.value = el.value.substring(0, start) + '  ' + el.value.substring(end);
    el.selectionStart = el.selectionEnd = start + 2;
    fsUpdateEditorStats();
  }
}
let FS_FONT_SIZE = 12.5;
function fsChangeFontSize(delta) {
  FS_FONT_SIZE = Math.max(10, Math.min(22, FS_FONT_SIZE + delta));
  document.getElementById('fsEditor').style.fontSize = FS_FONT_SIZE + 'px';
}

/* ══════════════════════════════════════════════════
   CENTRO DE NOTIFICACIONES (Activity Feed)
══════════════════════════════════════════════════ */
let NOTIFS = [];
function addNotif(title, timeStr, iconKind) {
  NOTIFS.unshift({ title, time: timeStr || new Date().toLocaleTimeString(), icon: iconKind || 'info' });
  if (NOTIFS.length > 25) NOTIFS.pop();
  renderNotifs();
  const dot = document.getElementById('notifDot');
  if (dot) dot.style.display = 'block';
}
function renderNotifs() {
  const list = document.getElementById('notifList');
  if (!list) return;
  if (!NOTIFS.length) {
    list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-dim);font-size:12px">Sin notificaciones pendientes</div>';
    return;
  }
  list.innerHTML = NOTIFS.map(n => `
    <div class="notif-item">
      <span style="color:#c084fc; font-size:14px">●</span>
      <div style="flex:1">
        <div>${escHtml(n.title)}</div>
        <div class="notif-time">${escHtml(n.time)}</div>
      </div>
    </div>`).join('');
}
function toggleNotifDropdown() {
  const d = document.getElementById('notifDropdown');
  const btn = document.getElementById('notifBtn');
  const open = d.style.display !== 'block';
  d.style.display = open ? 'block' : 'none';
  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  const dot = document.getElementById('notifDot');
  if (dot) dot.style.display = 'none';
}
function clearNotifs() {
  NOTIFS = [];
  renderNotifs();
  const dot = document.getElementById('notifDot');
  if (dot) dot.style.display = 'none';
}
document.addEventListener('click', e => {
  const d = document.getElementById('notifDropdown');
  const btn = document.getElementById('notifBtn');
  if (d && d.style.display === 'block' && !d.contains(e.target) && !btn.contains(e.target)) {
    d.style.display = 'none';
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }
});

const TAB_HASH = { consola: 'console', metricas: 'metrics', sistema: 'metrics', archivos: 'files', mundos: 'worlds', mca: 'worlds', backups: 'backups', tareas: 'tasks', historial: 'history', jugadores: 'players', inicio: 'home', mods: 'mods', flota: 'fleet', servidores: 'fleet', diagnostico: 'diag', configuracion: 'settings', ajustes: 'settings' };
const TAB_SLUG = { home: 'inicio', players: 'jugadores', mods: 'mods', console: 'consola', metrics: 'metricas', files: 'archivos', worlds: 'mundos', backups: 'backups', tasks: 'tareas', history: 'historial', fleet: 'flota', diag: 'diagnostico', settings: 'configuracion' };
function tabFromHash() {
  const h = (location.hash || '').replace(/^#/, '');
  const m = /^[a-z]+/.exec(h);
  const t = m ? (TAB_HASH[m[0]] || null) : null;
  if (t === 'files' && h.includes('/')) {
    try { fsPath = decodeURIComponent(h.split('/').slice(1).join('/')); }
    catch (e) { fsPath = h.split('/').slice(1).join('/'); }
  }
  return t;
}
function syncHash(name) {
  try {
    const want = '#' + (TAB_SLUG[name] || name);
    if ((location.hash || '').replace(/[^a-z]/g, '') !== want.slice(1)) history.replaceState(null, '', want);
  } catch (e) {}
}
window.addEventListener('hashchange', () => { const t = tabFromHash(); if (t) switchTab(t, true); });
// No consultar con la pestaña oculta: ahorra CPU del panel y del servidor
setInterval(() => { if (!document.hidden) refreshStats(); }, 2000);
setInterval(() => { if (!document.hidden) refreshConsole(); }, 2500);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refreshStats(); refreshConsole(); } });
refreshStats();
refreshConsole();

/* ══════════════════════════════════════════════════
   DIAGNOSTICS TAB — Crash-reports, JVM, Props, Logs
══════════════════════════════════════════════════ */
