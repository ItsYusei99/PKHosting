let autoScroll = true;
let historyBuffer = [];
let cmdHistory = [];
let cmdIndex = -1;

const SRV = new URLSearchParams(location.search).get('server') || '';
function U(p) {
  if (!SRV) return p;
  return p + (p.includes('?') ? '&' : '?') + 'server=' + encodeURIComponent(SRV);
}
const _fetch = window.fetch.bind(window);
window.fetch = async (...a) => {
  const r = await _fetch(...a);
  if (r.status === 401) location.reload();
  return r;
};
async function logout() {
  let auth = true;
  try {
    const r = await (await _fetch(U('/api/logout'), { method: 'POST' })).json();
    auth = r.auth !== false;
  } catch (e) { /* sin sesión que cerrar */ }
  if (!auth) {
    showToast('Sin contraseña: créala en Configuración para activar las sesiones', 'info');
    switchTab('settings');
    return;
  }
  location.href = '/login';
}
const SILEO_ICONS = {
  info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  loading: '<svg class="tk-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.22-8.56"/></svg>'
};
const SILEO_POS = ['top-left', 'top-center', 'top-right', 'bottom-left', 'bottom-center', 'bottom-right'];
let sileoDefaultPos = 'top-right';
function escToast(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function sileoBox(pos) {
  pos = SILEO_POS.includes(pos) ? pos : sileoDefaultPos;
  let box = document.querySelector(`.toaster[data-pos="${pos}"]`);
  if (!box) {
    box = document.createElement('div');
    box.className = 'toaster';
    box.dataset.pos = pos;
    box.setAttribute('aria-live', 'polite');
    document.body.appendChild(box);
  }
  return box;
}
function sileoShow({ title, description, type, duration, action, position } = {}) {
  type = SILEO_ICONS[type] ? type : 'info';
  if (duration === undefined) duration = 4000;
  const box = sileoBox(position);
  const el = document.createElement('div');
  el.className = 'toast';
  el.dataset.state = type;
  const icon = type === 'loading' ? SILEO_ICONS.loading : SILEO_ICONS[type];
  el.innerHTML = `<span class="tk-ico">${icon}</span>`
    + `<div class="tk-main"><div class="tk-title">${escToast(title || '')}</div>`
    + (description ? `<div class="tk-desc">${escToast(description)}</div>` : '')
    + (action ? `<button class="tk-act">${escToast(action.label)}</button>` : '')
    + `</div><button class="tk-x" title="Cerrar">×</button>`;
  let gone = false;
  const dismiss = () => {
    if (gone) return;
    gone = true;
    clearTimeout(timer);
    el.classList.add('exiting');
    setTimeout(() => el.remove(), 300);
  };
  let timer = null, left = duration, started = Date.now();
  const tick = () => {
    if (duration == null) return;
    clearTimeout(timer);
    timer = setTimeout(dismiss, left);
  };
  el.addEventListener('mouseenter', () => {
    clearTimeout(timer);
    if (duration != null) left -= Date.now() - started;
  });
  el.addEventListener('mouseleave', () => { started = Date.now(); tick(); });
  el.addEventListener('click', e => {
    if (e.target.closest('.tk-act') && action) { try { action.onClick(); } catch (err) {} }
    dismiss();
  });
  box.appendChild(el);
  while (box.children.length > 5) box.firstChild.remove();
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('ready')));
  if (duration != null) tick();
  return dismiss;
}
function showToast(msg, type) {
  sileoShow({ title: String(msg == null ? '' : msg), type: type || 'info' });
}
const sileo = {
  show: sileoShow,
  setPosition: pos => { if (SILEO_POS.includes(pos)) sileoDefaultPos = pos; },
  dismissAll: () => document.querySelectorAll('.toast').forEach(el => el.remove()),
  success: (title, desc, o) => sileoShow({ title, description: desc, type: 'success', ...(o || {}) }),
  error: (title, desc, o) => sileoShow({ title, description: desc, type: 'error', ...(o || {}) }),
  warning: (title, desc, o) => sileoShow({ title, description: desc, type: 'warning', ...(o || {}) }),
  info: (title, desc, o) => sileoShow({ title, description: desc, type: 'info', ...(o || {}) }),
  loading: (title, o) => sileoShow({ title, type: 'loading', duration: null, ...(o || {}) }),
  promise: async (p, { loading, success, error } = {}) => {
    const stop = sileoShow({ title: loading || 'Cargando…', type: 'loading', duration: null });
    try {
      const r = await p;
      stop();
      const msg = typeof success === 'function' ? success(r) : (success || 'Listo');
      sileoShow({ title: String(msg), type: 'success' });
      return r;
    } catch (e) {
      stop();
      const msg = typeof error === 'function' ? error(e) : (error || 'Falló la operación');
      sileoShow({ title: String(msg), type: 'error' });
      throw e;
    }
  }
};

let currentPublicIp = 'localhost:25566';
function copyIp() {
  navigator.clipboard.writeText(currentPublicIp);
  document.getElementById('copyBadge').textContent = '¡Copiado!';
  setTimeout(() => document.getElementById('copyBadge').textContent = 'Copiar', 1800);
  showToast('Dirección del servidor copiada: ' + currentPublicIp);
}
async function refreshPublicIp() {
  try {
    const res = await fetch(U('/api/tunnels'));
    const d = await res.json();
    if (d.public_ip) {
      currentPublicIp = d.public_ip;
      document.getElementById('ipText').textContent = d.public_ip;
    }
    const ps = document.getElementById('playitStatus');
    if (ps && d.playit) {
      const p = d.playit;
      ps.textContent = p.installed
        ? (p.service_active ? 'Agente playit: servicio activo. ' + (p.note || 'Pega arriba la dirección pública del túnel TCP (puerto 25566).') : 'Agente playit instalado pero servicio inactivo.')
        : 'playit no detectado en el sistema.';
    }
    const inp = document.getElementById('publicIpInput');
    if (inp && d.public_ip && !inp.value) inp.value = d.public_ip;
  } catch (e) {}
}
async function savePublicIp() {
  const v = document.getElementById('publicIpInput').value.trim();
  try {
    const res = await fetch(U('/api/public-ip'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ public_ip: v }) });
    const data = await res.json();
    showToast(data.msg || 'OK');
    refreshPublicIp();
  } catch (e) { showToast('Error al guardar IP'); }
}

const KNOWN_TABS = ['console', 'metrics', 'files', 'worlds', 'backups', 'tasks', 'history', 'fleet', 'diag', 'settings'];
function rememberTab(name) {
  try {
    localStorage.setItem('pkTab', name);
    if (name === 'files') localStorage.setItem('pkFs', fsPath);
  } catch (e) {}
}
function switchTab(name, fromHash) {
  if (!fromHash) syncHash(name);
  rememberTab(name);
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  
  const targetNav = Array.from(document.querySelectorAll('.nav-item')).find(el => el.textContent.toLowerCase().includes(
    name === 'console' ? 'consola' :
    name === 'metrics' ? 'métrica' :
    name === 'files' ? 'archivo' :
    name === 'worlds' ? 'mundo' :
    name === 'backups' ? 'backup' :
    name === 'tasks' ? 'tarea' :
    name === 'history' ? 'historial' :
    name === 'fleet' ? 'flota' :
    name === 'diag' ? 'diagnós' : 'config'
  ));
  if (targetNav) targetNav.classList.add('active');

  const tab = document.getElementById('tab-' + name);
  if (tab) tab.classList.add('active');

  if (name === 'metrics') { renderCharts(); loadHeat(); refreshPingMetrics(); }
  if (name === 'files') { loadFiles(); loadMods(); }
  if (name === 'worlds') { loadWorldManager(); }
  if (name === 'backups') loadBackups();
  if (name === 'tasks') loadSchedules();
  if (name === 'history') loadHistory();
  if (name === 'fleet') loadFleetView();
  if (name === 'diag') { loadCrashes(); jvmLoad(); propsLoad(); logsInit(); }
  if (name === 'console') { loadQuick(); loadModeration(); refreshCmdList(); }
  if (name === 'settings') { refreshPublicIp(); loadDiscord(); loadQuickCfg(); loadServers(); }
}

async function serverAction(act) {
  showToast('Enviando acción: ' + act.toUpperCase() + '...');
  try {
    const res = await fetch(U('/api/' + act), { method: 'POST' });
    const data = await res.json();
    showToast(data.msg || 'OK');
    setTimeout(refreshStats, 1200);
  } catch (e) {
    showToast('Error de conexión: ' + e);
  }
}

async function submitCmd() {
  const input = document.getElementById('cmdInput');
  const val = input.value.trim();
  if (!val) return;
  cmdHistory.push(val);
  cmdIndex = cmdHistory.length;
  input.value = '';
  refreshCmdList();
  try {
    const res = await fetch(U('/api/cmd'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd: val })
    });
    const data = await res.json();
    showToast(data.msg || (data.ok ? 'Comando enviado' : 'Error'));
    setTimeout(refreshConsole, 700);
  } catch (e) {
    showToast('Fallo al enviar comando', 'error');
  }
}

function handleCmdKey(e) {
  if (e.key === 'Enter') {
    submitCmd();
  } else if (e.key === 'ArrowUp') {
    if (cmdIndex > 0) {
      cmdIndex--;
      document.getElementById('cmdInput').value = cmdHistory[cmdIndex] || '';
    }
  } else if (e.key === 'ArrowDown') {
    if (cmdIndex < cmdHistory.length - 1) {
      cmdIndex++;
      document.getElementById('cmdInput').value = cmdHistory[cmdIndex] || '';
    } else {
      cmdIndex = cmdHistory.length;
      document.getElementById('cmdInput').value = '';
    }
  }
}

async function playerAction(act, name) {
  name = (name || '').trim();
  if (!name) { showToast('Jugador no válido'); return; }
  if ((act === 'kick' || act === 'ban') && !confirm(`¿${act === 'ban' ? 'BANEAR' : 'Expulsar'} a "${name}"?`)) return;
  try {
    const res = await fetch(U('/api/player'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: act, name })
    });
    const data = await res.json();
    showToast(data.msg || (data.ok ? 'OK' : 'Error'));
    setTimeout(refreshConsole, 700);
  } catch (e) {
    showToast('Fallo al enviar acción', 'error');
  }
}

function fmtSess(sec) {
  if (sec == null) return '';
  if (sec < 60) return 'menos de 1 min';
  const m = Math.floor(sec / 60), h = Math.floor(m / 60);
  if (h > 0) return h + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  return m + ' min';
}

let _wlOn = false;
async function loadModeration() {
  try {
    const d = await (await fetch(U('/api/lists'))).json();
    const chip = (n, act, danger) => `<button class="p-chip"${danger ? ' style="color:#f87171;border-color:#7f1d1d"' : ''} title="${act}" onclick="modAct('${act}','${n.replace(/'/g, "")}')"><img src="https://mc-heads.net/avatar/${encodeURIComponent(n)}/18" class="p-avatar" style="width:16px;height:16px" onerror="this.style.display='none'">${n} ✕</button>`;
    document.getElementById('modOps').innerHTML = (d.ops && d.ops.length) ? d.ops.map(n => chip(n, 'deop')).join(' ') : '—';
    document.getElementById('modBans').innerHTML = (d.banned && d.banned.length) ? d.banned.map(b => chip(b.name, 'pardon', true)).join(' ') : '—';
    _wlOn = !!d.whitelist_on;
    document.getElementById('wlState').textContent = _wlOn ? '[ON]' : '[OFF]';
    document.getElementById('modWl').innerHTML = (d.whitelist && d.whitelist.length) ? d.whitelist.map(n => chip(n, 'wlremove')).join(' ') : '—';
  } catch (e) {}
}
async function loadHistory() {
  try {
    const h = await (await fetch(U('/api/connections'))).json();
    const box = document.getElementById('connHist');
    const items = (h && h.items) || [];
    const sub = document.getElementById('histSub');
    if (sub) sub.textContent = items.length ? `${items.length} registros · Entradas y salidas del servidor` : 'Entradas y salidas del servidor';
    if (!items.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin registros</span></div>'; return; }
    box.innerHTML = items.slice(0, 30).map(e => {
      const dt = new Date(e.t * 1000).toLocaleString();
      const what = e.ev === 'join' ? 'entró' : `salió (${fmtSess(e.dur || 0)})`;
      const dot = e.ev === 'join' ? '#34d399' : '#f87171';
      return `<div class="file-row"><img src="https://mc-heads.net/avatar/${encodeURIComponent(e.name)}/22" class="p-avatar" style="width:22px;height:22px" onerror="this.style.display='none'"><span class="file-icon" style="color:${dot}" title="${e.ev === 'join' ? 'Entrada' : 'Salida'}">●</span><span class="file-name" style="cursor:pointer" onclick="openPlayerModal('${e.name}')">${e.name} ${what}</span><span class="file-size">${dt}</span></div>`;
    }).join('');
  } catch (e) {}
}
async function modAct(act, name) {
  if ((act === 'wlremove') && !confirm(`¿Quitar a "${name}" de la whitelist?`)) return;
  try {
    const r = await (await fetch(U('/api/player'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: act, name }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadModeration();
}
async function wlAdd() {
  const v = document.getElementById('wlName').value.trim();
  if (!v) return;
  try {
    const r = await (await fetch(U('/api/player'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'wladd', name: v }) })).json();
    showToast(r.msg || 'OK');
    document.getElementById('wlName').value = '';
  } catch (e) { showToast('Error'); }
  loadModeration();
}
async function wlToggle() {
  try {
    const r = await (await fetch(U('/api/whitelist'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: !_wlOn }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  setTimeout(loadModeration, 1200);
}

function clearTerminal() {
  document.getElementById('termBody').innerHTML = '';
}

let stickBottom = true;
function paintAutoBtn() {
  document.getElementById('btnAutoScroll').textContent =
    !autoScroll ? 'Auto-scroll: OFF' : (stickBottom ? 'Auto-scroll: ON' : 'Ir abajo ↓');
}
function toggleAutoScroll() {
  autoScroll = !autoScroll;
  if (autoScroll) {
    stickBottom = true;
    const body = document.getElementById('termBody');
    body.scrollTop = body.scrollHeight;
  }
  paintAutoBtn();
}
document.getElementById('termBody').addEventListener('scroll', () => {
  const body = document.getElementById('termBody');
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  if (atBottom !== stickBottom) {
    stickBottom = atBottom;
    paintAutoBtn();
  }
});

function copyLogs() {
  const t = document.getElementById('termBody').innerText;
  navigator.clipboard.writeText(t);
  showToast('Registro de consola copiado al portapapeles');
}

function highlightLogLine(line) {
  const esc = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  if (esc.includes('Done (')) return `<span class="log-done">${esc}</span>`;
  if (esc.includes('ERROR') || esc.includes('FATAL') || esc.includes('Exception')) return `<span class="log-err">${esc}</span>`;
  if (esc.includes('WARN')) return `<span class="log-warn">${esc}</span>`;
  return `<span class="log-info">${esc}</span>`;
}

let termLines = [];
let logFilter = '';
const CMD_BASE = ['help', 'list', 'say ', 'op ', 'deop ', 'kick ', 'ban ', 'pardon ', 'stop', 'save-all', 'save-on', 'save-off', 'reload', 'whitelist ', 'start', 'restart', 'reload', 'kill', 'tick query', 'profile entities'];
function setLogFilter(f) {
  logFilter = (logFilter === f) ? '' : f;
  renderConsole();
}
function renderConsole() {
  const q = (document.getElementById('logSearch').value || '').toLowerCase();
  const body = document.getElementById('termBody');
  const rows = termLines.filter(l => {
    if (logFilter === 'INFO' && !l.includes('INFO')) return false;
    if (logFilter === 'WARN' && !l.includes('WARN')) return false;
    if (logFilter === 'ERROR' && !(l.includes('ERROR') || l.includes('FATAL') || l.includes('Exception'))) return false;
    if (q && !l.toLowerCase().includes(q)) return false;
    return true;
  });
  const prevH = body.scrollHeight, prevT = body.scrollTop;
  body.innerHTML = rows.length ? rows.map(highlightLogLine).join('<br>') : '<span class="log-info">Sin coincidencias</span>';
  if (autoScroll && stickBottom) {
    body.scrollTop = body.scrollHeight;
  } else {
    body.scrollTop = Math.max(0, prevT + (body.scrollHeight - prevH));
  }
}
async function refreshConsole() {
  try {
    const res = await fetch(U('/api/console'));
    const data = await res.json();
    if (data.lines && data.lines.length) {
      termLines = data.lines;
      renderConsole();
    }
  } catch (e) {}
}
function refreshCmdList() {
  const seen = new Set();
  const all = CMD_BASE.concat(cmdHistory.slice().reverse()).filter(c => {
    const k = c.trim().toLowerCase();
    if (!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  }).slice(0, 30);
  document.getElementById('cmdList').innerHTML = all.map(c => `<option value="${c.replace(/"/g, '&quot;')}">`).join('');
}
async function loadQuick() {
  try {
    const d = await (await fetch(U('/api/uiconfig'))).json();
    const box = document.getElementById('quickCmds');
    box.innerHTML = ((d && d.quick_commands) || []).map(c =>
      `<button class="term-tool-btn" onclick="quickSend(this.dataset.c)" data-c="${c.replace(/"/g, '&quot;')}">${c.replace(/</g, '&lt;')}</button>`
    ).join('');
  } catch (e) {}
}
async function quickSend(c) {
  try {
    const r = await (await fetch(U('/api/cmd'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cmd: c }) })).json();
    showToast(r.msg || 'OK');
    setTimeout(refreshConsole, 700);
  } catch (e) { showToast('Error'); }
}
async function loadQuickCfg() {
  try {
    const d = await (await fetch(U('/api/uiconfig'))).json();
    document.getElementById('quickInput').value = ((d && d.quick_commands) || []).join(', ');
  } catch (e) {}
}
async function saveQuick() {
  const v = document.getElementById('quickInput').value.split(',').map(s => s.trim()).filter(Boolean).slice(0, 12);
  try {
    const r = await (await fetch(U('/api/uiconfig'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ quick_commands: v }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadQuick();
}

async function refreshStats() {
  try {
    const res = await fetch(U('/api/stats'));
    const d = await res.json();
    
    // Status Badge
    const badge = document.getElementById('statusBadge');
    const stText = document.getElementById('statusText');
    const sideDot = document.getElementById('sideDot');
    badge.className = 'status-pill ' + d.status;
    stText.textContent = d.status;
    
    const colorMap = {
      'RUNNING': 'var(--color-online)',
      'STARTING': 'var(--color-starting)',
      'STOPPING': 'var(--color-stopping)',
      'OFFLINE': 'var(--color-offline)'
    };
    sideDot.style.background = colorMap[d.status] || 'var(--color-offline)';

    // Buttons Disable
    document.getElementById('btnStart').disabled = (d.status !== 'OFFLINE');
    document.getElementById('btnStop').disabled = (d.status === 'OFFLINE');
    document.getElementById('btnRestart').disabled = (d.status === 'OFFLINE');
    document.getElementById('btnReload').disabled = (d.status !== 'RUNNING');
    document.getElementById('btnKill').disabled = (d.status === 'OFFLINE');
    if (d.public_ip) {
      currentPublicIp = d.public_ip;
      const ipEl = document.getElementById('ipText');
      if (ipEl) ipEl.textContent = d.public_ip;
    }

    // Cards
    document.getElementById('cardUptime').textContent = d.uptime;
    document.getElementById('cardUptimeSub').textContent = d.status === 'RUNNING' ? 'En línea y respondiendo' : (d.status === 'STARTING' ? 'Cargando mods y mundo...' : 'Servidor detenido');
    
    // Color por nivel de uso: verde < umbral1, ámbar < umbral2, rojo arriba
    const cpuPct = Math.max(0, d.cpu || 0);
    const memPct = d.max_mem_gb ? (d.mem_gb / d.max_mem_gb) * 100 : 0;
    const cpuColor = usageColor(cpuPct, 50, 80);
    const memColor = usageColor(memPct, 60, 85);
    cpuStrokeColor = cpuColor; memStrokeColor = memColor;

    const cardCpu = document.getElementById('cardCpu');
    cardCpu.textContent = d.cpu.toFixed(1) + '%';
    paintUsage(cardCpu, document.getElementById('barCpu'), Math.min(100, cpuPct), cpuColor);

    const cardMem = document.getElementById('cardMem');
    cardMem.textContent = (d.mem_mb >= 1024 ? d.mem_gb + ' GB' : d.mem_mb + ' MB');
    paintUsage(cardMem, document.getElementById('barMem'), Math.min(100, memPct), memColor);
    document.getElementById('cardMemSub').textContent = `Límite: ${d.max_mem_gb} GB (${d.mem_mb} MB)`;

    document.getElementById('cardPlayers').textContent = `${d.online_players} / ${d.max_players}`;
    const mgmt = document.getElementById('playerMgmt');
    const rows = document.getElementById('playerRows');
    const hasPlayers = d.online_players > 0 && d.player_names && d.player_names.length;
    if (mgmt) mgmt.style.display = hasPlayers ? 'block' : 'none';
    if (rows && hasPlayers) {
      const sess = d.sessions || {};
      rows.innerHTML = d.player_names.map(n => {
        const safe = n.replace(/'/g, "").replace(/"/g, '&quot;');
        const t = sess[n] != null ? ` · <span style="font-size:11px;color:var(--text-dim)">${fmtSess(sess[n])}</span>` : '';
        return `<div class="file-row">
          <img class="p-avatar" src="https://mc-heads.net/avatar/${encodeURIComponent(n)}/28" alt="${safe}" onerror="this.src='/icon.svg'">
          <span class="file-name" style="cursor:pointer" onclick="openPlayerModal('${safe}')" title="Ver ficha del jugador"><b>${safe}</b>${t}</span>
          <span class="file-actions">
            <button class="cmd-btn" title="Ficha del jugador" onclick="openPlayerModal('${safe}')">Ficha</button>
            <button class="term-tool-btn" title="Dar OP" onclick="playerAction('op', '${safe}')">OP</button>
            <button class="term-tool-btn" title="Quitar OP" onclick="playerAction('deop', '${safe}')">DeOP</button>
            <button class="term-tool-btn" title="Expulsar" onclick="playerAction('kick', '${safe}')">Kick</button>
            <button class="term-tool-btn" style="color:#f87171; border-color:#7f1d1d" title="Banear" onclick="playerAction('ban', '${safe}')">Ban</button>
          </span>
        </div>`;
      }).join('');
    }

    // Metrics tab badges
    paintBadge(document.getElementById('chartCpuBadge'), d.cpu.toFixed(1) + '%', cpuColor);
    paintBadge(document.getElementById('chartMemBadge'), d.mem_gb + ' GB', memColor);

    // TPS + MSPT (muestreo `tick query` cada 60 s)
    const t = d.tps || {};
    const tpsVal = document.getElementById('tpsVal');
    if (t.ok && t.tps != null) {
      const tc = t.tps >= 19 ? '#34d399' : (t.tps >= 15 ? '#fbbf24' : '#f87171');
      tpsVal.textContent = t.tps.toFixed(1);
      tpsVal.style.backgroundImage = `linear-gradient(180deg, #ffffff 15%, ${tc} 95%)`;
      tpsVal.style.filter = `drop-shadow(0 0 14px ${tc}59)`;
      document.getElementById('tpsBadge').textContent = t.tps >= 19 ? 'ESTABLE' : (t.tps >= 15 ? 'CARGADO' : 'SOBRECARGADO');
      paintBadge(document.getElementById('tpsBadge'), document.getElementById('tpsBadge').textContent, tc);
      document.getElementById('tpsSub').textContent = `MSPT: ${(t.mspt != null ? t.mspt.toFixed(2) : '—')} ms · objetivo 50 ms/tick`;
      const bar = document.getElementById('tpsBar');
      bar.style.width = Math.min(100, (t.tps / 20) * 100) + '%';
      bar.style.background = `linear-gradient(90deg, ${tc}99, ${tc})`;
    } else {
      tpsVal.textContent = d.status === 'RUNNING' ? '…' : '—';
      document.getElementById('tpsSub').textContent = d.status === 'RUNNING' ? 'Muestreando…' : 'Servidor apagado';
    }

    const perfBanner = document.getElementById('perfBanner');
    if (perfBanner) {
      if (d.status === 'RUNNING' && ((t.ok && t.tps != null && t.tps < 16.0) || memPct > 90)) {
        perfBanner.style.display = 'flex';
        document.getElementById('perfBannerMsg').textContent = (t.ok && t.tps < 16.0)
          ? `TPS crítico (${t.tps.toFixed(1)} / 20) detectado.`
          : `Uso de memoria RAM elevado (${memPct.toFixed(1)}%).`;
      } else {
        perfBanner.style.display = 'none';
      }
    }

    document.getElementById('detStatus').textContent = d.status;
    document.getElementById('detPid').textContent = d.pid || '-';
    document.getElementById('detLoad').textContent = d.system_load;
    document.getElementById('detWorld').textContent = d.world_gb + ' GB';
    document.getElementById('detDisk').textContent = (d.disk_gb != null ? d.disk_gb + ' GB' : '—');
    document.getElementById('detNet').textContent = (d.net ? d.net[0] + ' / ' + d.net[1] + ' kB/s' : '—');

    historyBuffer = d.history || [];
    if (document.getElementById('tab-metrics').classList.contains('active')) {
      renderCharts();
    }
  } catch (e) {}
}

function drawSmoothChart(canvasId, values, maxVal, colorStroke, colorFill, unit) {
  const c = document.getElementById(canvasId);
  if (!c) return;
  const ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth;
  const h = c.clientHeight;
  c.width = w * dpr;
  c.height = h * dpr;
  ctx.scale(dpr, dpr);

  ctx.clearRect(0, 0, w, h);
  if (!values || values.length < 2) {
    ctx.fillStyle = '#64748b';
    ctx.font = '12px Inter';
    ctx.fillText('Esperando datos de telemetría...', 20, h / 2);
    return;
  }

  // Gridlines
  ctx.strokeStyle = 'rgba(255,255,255,0.05)';
  ctx.lineWidth = 1;
  for (let y = 0; y <= 4; y++) {
    const yPos = (h / 4) * y;
    ctx.beginPath();
    ctx.moveTo(0, yPos);
    ctx.lineTo(w, yPos);
    ctx.stroke();
  }

  const step = w / (values.length - 1);
  ctx.beginPath();
  ctx.moveTo(0, h - (values[0] / maxVal) * h);

  for (let i = 1; i < values.length; i++) {
    const prevX = (i - 1) * step;
    const prevY = h - (values[i - 1] / maxVal) * h;
    const currX = i * step;
    const currY = h - (values[i] / maxVal) * h;
    const midX = (prevX + currX) / 2;
    ctx.bezierCurveTo(midX, prevY, midX, currY, currX, currY);
  }

  // Stroke con resplandor
  ctx.save();
  ctx.strokeStyle = colorStroke;
  ctx.lineWidth = 2.5;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.shadowColor = colorStroke;
  ctx.shadowBlur = 12;
  ctx.stroke();
  ctx.restore();

  // Fill
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, colorFill);
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = grad;
  ctx.fill();
}

function chartPeakAvg(vals, digits) {
  if (!vals.length) return 'pico — · prom —';
  const mx = Math.max(...vals);
  const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
  return `pico ${mx.toFixed(digits)} · prom ${avg.toFixed(digits)}`;
}

let cpuStrokeColor = '#a855f7', memStrokeColor = '#7c3aed';
function usageColor(pct, warnAt, critAt) {
  if (pct >= critAt) return '#f87171';
  if (pct >= warnAt) return '#fbbf24';
  return '#34d399';
}
function paintUsage(valEl, barEl, pct, color) {
  // Solo el TEXTO de color: backgroundImage no resetea background-clip:text
  // (el shorthand `background` sí lo hacía y pintaba toda la caja como barra).
  valEl.style.backgroundImage = `linear-gradient(180deg, #ffffff 15%, ${color} 95%)`;
  valEl.style.webkitBackgroundClip = 'text';
  valEl.style.backgroundClip = 'text';
  valEl.style.filter = `drop-shadow(0 0 14px ${color}59)`;
  barEl.style.width = pct + '%';
  barEl.style.background = `linear-gradient(90deg, ${color}99, ${color})`;
}
function paintBadge(el, text, color) {
  if (!el) return;
  el.textContent = text;
  el.style.color = color;
  el.style.borderColor = color + '66';
}
let h24At = 0;
async function renderCharts() {
  if (Date.now() - h24At > 60000) {
    h24At = Date.now();
    try {
      const h = await (await fetch(U('/api/history'))).json();
      if (h.cpu && h.cpu.length > 1) {
        drawSmoothChart('canvasH24Cpu', h.cpu, 100.0, '#a855f7', 'rgba(168,85,247,0.25)', '%');
        drawSmoothChart('canvasH24Mem', h.mem, 8.0, '#7c3aed', 'rgba(124,58,237,0.30)', 'GB');
        document.getElementById('h24CpuBadge').textContent = h.cpu[h.cpu.length - 1].toFixed(1) + '%';
        const mm = h.mem[h.mem.length - 1];
        document.getElementById('h24MemBadge').textContent = mm + ' GB';
      }
    } catch (e) {}
  }
  if (!historyBuffer || historyBuffer.length < 2) return;
  const cpuVals = historyBuffer.map(h => h.cpu || 0);
  const memVals = historyBuffer.map(h => (h.mem || 0) / 1024.0);
  drawSmoothChart('canvasCpu', cpuVals, 100.0, cpuStrokeColor, cpuStrokeColor + '40', '%');
  drawSmoothChart('canvasMem', memVals, 8.0, memStrokeColor, memStrokeColor + '4D', 'GB');
  const cp = document.getElementById('chartCpuPeak');
  if (cp) cp.textContent = chartPeakAvg(cpuVals, 1) + '%';
  const mp = document.getElementById('chartMemPeak');
  if (mp) mp.textContent = chartPeakAvg(memVals, 2) + ' GB';
}

let fsPath = '';
let fsSort = { key: 'name', dir: 1, group: true };
function fsJoin(base, name) { return (base ? base + '/' : '') + name; }
function escHtml(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
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
          ? `<a href="#" data-path="${full}" onclick="fsEnter(this.dataset.path);return false" style="color:#93c5fd">${escHtml(f.name)}/</a>`
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
function fsNavUp() { fsNav(fsPath.split('/').slice(0, -1).join(''), true); }
window.addEventListener('popstate', (e) => {
  const p = (e.state && e.state.fsPath !== undefined) ? e.state.fsPath : '';
  fsPath = p;
  loadFiles(false);
});
try { history.replaceState({ fsPath: '' }, '', location.pathname + location.hash); } catch (err) {}
async function fsEdit(p) {
  document.getElementById('fsEditPath').textContent = p;
  document.getElementById('fsEditor').value = 'Cargando...';
  document.getElementById('fsEditModal').style.display = 'flex';
  const r = await fsPost('read', { path: p });
  document.getElementById('fsEditor').value = r.ok ? (r.content || '') : ('Error: ' + (r.msg || 'no se pudo leer'));
}
function fsCloseEdit() { document.getElementById('fsEditModal').style.display = 'none'; }
async function fsSaveEdit() {
  const p = document.getElementById('fsEditPath').textContent;
  const r = await fsPost('write', { path: p, content: document.getElementById('fsEditor').value });
  showToast(r.msg || 'OK'); if (r.ok) { fsCloseEdit(); loadFiles(false); }
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
  const v = (prompt(kind === 'dir' ? 'Nombre de la nueva carpeta:' : 'Nombre del nuevo archivo:') || '').trim();
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
  showToast('Subiendo ' + inp.files.length + ' archivo(s)...');
  const uz = document.getElementById('fsUnzip') && document.getElementById('fsUnzip').checked ? '&unzip=1' : '';
  try {
    const res = await fetch(U('/api/upload?path=' + encodeURIComponent(fsPath) + uz), { method: 'POST', body: fd });
    const r = await res.json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error al subir'); }
  inp.value = '';
  loadFiles();
}
function fsDownload(p) { window.open(U('/api/download?path=' + encodeURIComponent(p)), '_blank'); }
async function fsRename(p) {
  const dest = (prompt('Renombrar / mover a (ruta relativa):', p) || '').trim();
  if (!dest || dest === p) return;
  const r = await fsPost('rename', { path: p, dest });
  showToast(r.msg || 'OK'); loadFiles();
}
async function fsDelete(p) {
  if (!confirm('¿Eliminar "' + p + '"?')) return;
  const r = await fsPost('delete', { path: p });
  showToast(r.msg || 'OK'); loadFiles();
}

const BK_SVG_DL = '<svg class="ico" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
const BK_SVG_BACK = '<svg class="ico" viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>';
const BK_SVG_TRASH = '<svg class="ico" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';
function fmtBytes(b) {
  if (b == null) return '—';
  if (b > 1e9) return (b / 1e9).toFixed(2) + ' GB';
  if (b > 1e6) return Math.round(b / 1e6) + ' MB';
  return Math.round(b / 1024) + ' KB';
}
function bkNextRun(timeStr) {
  const m = /^([01][0-9]|2[0-3]):([0-5][0-9])$/.exec(timeStr || '');
  if (!m) return null;
  const n = new Date();
  const t = new Date(n);
  t.setHours(parseInt(m[1]), parseInt(m[2]), 0, 0);
  if (t <= n) t.setDate(t.getDate() + 1);
  return t;
}
function bkCountdown(t) {
  const s = Math.max(0, Math.round((t - Date.now()) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h > 0) return `en ${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `en ${m} min`;
  return 'en menos de 1 min';
}
async function loadBackups() {
  try {
    const res = await fetch(U('/api/backups'));
    const d = await res.json();
    const items = (d.items || []).slice().reverse();
    // Resumen superior
    if (d.enabled) {
      const nx = bkNextRun(d.time);
      document.getElementById('bkNext').textContent = nx ? bkCountdown(nx) : '—';
      document.getElementById('bkNextSub').textContent = nx ? `hoy/mañana · ${d.time}` : `hora inválida (${d.time})`;
    } else {
      document.getElementById('bkNext').textContent = 'Desactivado';
      document.getElementById('bkNextSub').textContent = 'actívalo en Configuración avanzada';
    }
    document.getElementById('bkSpace').textContent = fmtBytes(d.total_b || 0);
    document.getElementById('bkSpaceSub').textContent = `libre en disco: ${d.free_gb} GB`;
    document.getElementById('bkCount').textContent = items.length;
    document.getElementById('bkCountSub').textContent = `${d.monthly || 0} mensuales eternos`;
    const w = d.writable ? '✔ destino escribible' : '✘ destino NO escribible' + (d.writable_msg ? ': ' + d.writable_msg : '');
    document.getElementById('bkStatus').textContent =
      `${d.dir} · ret ${d.retention_days}d${d.keep_monthly ? ' + mensual' : ''} · ${w}`;
    const job = document.getElementById('bkJob');
    job.textContent = (d.job && d.job.running) ? ('⏳ ' + (d.job.msg || 'trabajando...')) : ((d.job && d.job.msg) ? d.job.msg : '');
    const wrap = document.getElementById('bkProgWrap');
    const showBar = !!(d.job && (d.job.running || (d.job.pct > 0 && d.job.stage && d.job.stage !== '')));
    wrap.style.display = showBar ? 'block' : 'none';
    if (showBar) {
      const pct = Math.max(0, Math.min(100, d.job.pct || 0));
      document.getElementById('bkBar').style.width = pct + '%';
      document.getElementById('bkPct').textContent = pct + '%';
      document.getElementById('bkStage').textContent = d.job.stage || d.job.job || 'trabajando...';
      if (d.job.running) { clearTimeout(window._bkT); window._bkT = setTimeout(loadBackups, 1500); }
    }
    const box = document.getElementById('bkList');
    if (!items.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin backups todavía — pulsa «Backup ahora»</span></div>'; }
    else {
      box.innerHTML = items.map(f => `
        <div class="file-row" style="align-items:flex-start; padding:14px 18px">
          <span class="file-icon is-file" title="Backup">${BK_SVG_DL}</span>
          <span class="file-name" style="font-size:13.5px">${f.name}${f.monthly ? ' <span class="chart-badge sub">MENSUAL</span>' : ''}<br><span style="font-size:11.5px;color:var(--text-dim)">${f.date} · ${f.size}</span></span>
          <span class="file-actions" style="gap:8px">
          <button class="btn-ghost" onclick="bkDownload('${f.name}')">Descargar</button>
          <button class="btn-ghost" onclick="restoreBackup('${f.name}')">Restaurar</button>
            <button class="icon-btn danger" title="Eliminar" onclick="deleteBackup('${f.name}')">${BK_SVG_TRASH}</button>
          </span>
        </div>`).join('');
    }
    try {
      const c = await (await fetch(U('/api/backup-cfg'))).json();
      const cfg = (c && c.config) || {};
      document.getElementById('bkEn').checked = !!cfg.backup_enabled;
      if (!document.getElementById('bkTime').value) document.getElementById('bkTime').value = cfg.backup_time || '04:00';
      if (!document.getElementById('bkDir').value) document.getElementById('bkDir').value = cfg.backup_dir || '';
      const bkRetEl = document.getElementById('bkRet');
      if (!bkRetEl.dataset.dirty) bkRetEl.value = cfg.retention_days || 7;
      document.getElementById('bkMonthly').checked = cfg.keep_monthly !== false;
      const ret = cfg.retention_days || 7;
      document.getElementById('bkRetNote').textContent =
        `Se conservan los últimos ${ret} días; ${cfg.keep_monthly !== false ? 'el último de cada mes se guarda para siempre (MENSUAL).' : 'sin copias mensuales.'}`;
    } catch (e) {}
  } catch (e) {
    document.getElementById('bkList').innerHTML = '<div class="file-row">Error al cargar backups</div>';
  }
}
async function backupNow() {
  try {
    await sileo.promise(
      fetch(U('/api/backup-now'), { method: 'POST' }).then(async r => {
        const d = await r.json();
        if (!d.ok) throw new Error(d.msg || 'Error');
        return d.msg || 'Backup iniciado';
      }),
      { loading: 'Iniciando backup…', success: m => m, error: e => e.message });
  } catch (e) {}
  setTimeout(loadBackups, 1500);
}
async function restoreBackup(name) {
  if (!confirm(`¿RESTAURAR "${name}"?

Detiene el servidor, guarda un pre-backup de seguridad y sobrescribe el mundo actual.`)) return;
  try {
    await sileo.promise(
      fetch(U('/api/restore'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }).then(async r => {
        const d = await r.json();
        if (!d.ok) throw new Error(d.msg || 'Error');
        return d.msg || 'Restaurando…';
      }),
      { loading: `Restaurando ${name}…`, success: m => m, error: e => e.message });
  } catch (e) {}
  setTimeout(loadBackups, 2000);
}
function bkDownload(name) { window.open(U('/api/backup-download?name=' + encodeURIComponent(name)), '_blank'); }
async function saveBkCfg() {
  const body = { backup_enabled: document.getElementById('bkEn').checked,
    backup_time: document.getElementById('bkTime').value.trim(),
    backup_dir: document.getElementById('bkDir').value.trim(),
    retention_days: parseInt(document.getElementById('bkRet').value) || 7,
    keep_monthly: document.getElementById('bkMonthly').checked };
  try {
    const r = await (await fetch(U('/api/backup-cfg'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadBackups();
}
async function deleteBackup(name) {
  if (!confirm(`¿Eliminar el backup "${name}"?`)) return;
  try {
    const r = await (await fetch(U('/api/backup-delete'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error al eliminar'); }
  loadBackups();
}
setInterval(() => { const t = document.getElementById('tab-backups'); if (t && t.classList.contains('active')) loadBackups(); }, 5000);
setInterval(() => { const t = document.getElementById('tab-console'); if (t && t.classList.contains('active')) loadModeration(); }, 10000);
setInterval(() => { const t = document.getElementById('tab-history'); if (t && t.classList.contains('active')) loadHistory(); }, 10000);

async function refreshTps() {
  showToast('Muestreando TPS...');
  try {
    const r = await (await fetch(U('/api/tps'), { method: 'POST' })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error al muestrear'); }
}

async function changePw() {
  const cur = document.getElementById('pwCur').value;
  const nw = document.getElementById('pwNew').value;
  try {
    const r = await (await fetch(U('/api/password'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ current: cur, new: nw }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
    document.getElementById('pwCur').value = '';
    document.getElementById('pwNew').value = '';
    if (r.login) setTimeout(() => { location.href = '/login'; }, 1200);
    else loadServers();
  } catch (e) { showToast('Error', 'error'); }
}

const DC_LABELS = { up: 'Servidor en línea', down: 'Servidor detenido', tps: 'TPS bajo', backup: 'Backup fallido', join: 'Jugador entra', leave: 'Jugador sale' };
async function loadDiscord() {
  try {
    const d = await (await fetch(U('/api/discord'))).json();
    const c = (d && d.config) || {};
    const inp = document.getElementById('dcHook');
    if (inp && !inp.value) inp.value = c.webhook || '';
    const box = document.getElementById('dcEvents');
    const ev = c.events || {};
    box.innerHTML = Object.keys(DC_LABELS).map(k =>
      `<label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" data-ev="${k}"${ev[k] ? ' checked' : ''}> ${DC_LABELS[k]}</label>`
    ).join('');
  } catch (e) {}
}
async function saveDiscord() {
  const ev = {};
  document.querySelectorAll('#dcEvents input[data-ev]').forEach(el => ev[el.dataset.ev] = el.checked);
  try {
    const r = await (await fetch(U('/api/discord'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ webhook: document.getElementById('dcHook').value.trim(), events: ev }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
}
async function testDiscord() {
  try {
    const r = await (await fetch(U('/api/discord-test'), { method: 'POST' })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
}
function skWhen() {
  return document.getElementById('skMode').value === 'interval'
    ? { every_min: parseInt(document.getElementById('skWhen').value) || 60 }
    : { at: document.getElementById('skWhen').value.trim() || '04:00' };
}
async function loadSchedules() {
  try {
    const d = await (await fetch(U('/api/schedules'))).json();
    const box = document.getElementById('skList');
    const items = (d && d.tasks) || [];
    if (!items.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin tareas — el reinicio automático está desactivado hasta que crees una</span></div>'; return; }
    box.innerHTML = items.map(t => {
      const when = t.mode === 'interval' ? `cada ${t.every_min} min` : `diaria ${t.at}`;
      return `<div class="file-row">
        <span class="file-name">${t.name} <span class="chart-badge sub">${t.kind}</span><br><span style="font-size:11px;color:var(--text-dim)">${when} · ${t.enabled ? 'activa' : 'pausada'}</span></span>
        <span class="file-actions">
          <button class="term-tool-btn" title="Ejecutar ahora" onclick="skAct('run','${t.id}')">Ahora</button>
          <button class="term-tool-btn" title="Activar/pausar" onclick="skAct('toggle','${t.id}')">${t.enabled ? 'Pausar' : 'Activar'}</button>
          <button class="icon-btn danger" title="Eliminar" onclick="skAct('delete','${t.id}')">${BK_SVG_TRASH}</button>
        </span>
      </div>`;
    }).join('');
  } catch (e) {
    document.getElementById('skList').innerHTML = '<div class="file-row">Error al cargar tareas</div>';
  }
}
async function skCreate() {
  const body = { name: document.getElementById('skName').value.trim() || 'Tarea',
    kind: document.getElementById('skKind').value, mode: document.getElementById('skMode').value,
    payload: document.getElementById('skPayload').value.trim(),
    warn_min: parseInt(document.getElementById('skWarn').value) || 0, ...skWhen() };
  if (body.kind === 'restart' && !confirm('¿Crear tarea de REINICIO automático? (vendrá activada; puedes pausarla cuando quieras)')) return;
  try {
    const r = await (await fetch(U('/api/schedule'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'create', ...body }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadSchedules();
}
async function skAct(action, id) {
  if (action === 'delete' && !confirm('¿Eliminar tarea?')) return;
  try {
    const r = await (await fetch(U('/api/schedule'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, id }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadSchedules();
}

let MODS_CACHE = [];
let MODS_VIEW = [];
async function loadMods() {
  try {
    const d = await (await fetch(U('/api/mods'))).json();
    MODS_CACHE = (d && d.mods) || [];
    renderMods();
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
    const r = await (await fetch(U('/api/mod'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'toggle', name: m.name }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
    if (r.ok === false) { c.enabled = !c.enabled; renderMods(); }
  } catch (e) { c.enabled = !c.enabled; renderMods(); showToast('Error de conexión', 'error'); }
}
async function modDelIdx(i) {
  const m = MODS_VIEW[i];
  if (!m) return;
  if (!confirm(`¿Eliminar el mod "${m.name}"?`)) return;
  try {
    const r = await (await fetch(U('/api/mod'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'delete', name: m.name }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modToggle(name) { // compatibilidad
  try {
    const r = await (await fetch(U('/api/mod'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'toggle', name }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modDel(name) { // compatibilidad
  if (!confirm(`¿Eliminar el mod "${name}"?`)) return;
  try {
    const r = await (await fetch(U('/api/mod'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'delete', name }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modBulk(enable) {
  const n = MODS_CACHE.filter(m => m.enabled !== enable).length;
  if (!n) { showToast('Nada que cambiar', 'info'); return; }
  if (!confirm(`${enable ? 'Activar' : 'Desactivar'} ${n} mod(s)? Requiere reinicio del servidor.`)) return;
  try {
    const r = await (await fetch(U('/api/mod'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: enable ? 'enable_all' : 'disable_all' }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
let SRV_CACHE = [];
async function loadServers() {
  const box = document.getElementById('srvList');
  if (!box) return;
  try {
    const d = await (await fetch(U('/api/servers'))).json();
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
        ${s.current ? '' : `<button class="term-tool-btn" onclick="srvOpen('${s.id}')">Abrir</button>`}
        <button class="icon-btn" title="Editar" onclick="srvShowForm('${s.id}')">${SVG_PENCIL}</button>
        <button class="icon-btn danger" title="Eliminar" onclick="srvDelete('${s.id}')">${SVG_TRASH}</button>
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
    const r = await (await fetch(U('/api/servers'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
    if (r.ok !== false) srvHideForm();
  } catch (e) { showToast('Error de conexión', 'error'); }
  loadServers();
}
async function srvDelete(id) {
  const s = SRV_CACHE.find(x => x.id === id);
  if (!s) return;
  if (!confirm(`¿Quitar '${s.name}' del panel? No se borran sus archivos.`)) return;
  try {
    const r = await (await fetch(U('/api/servers'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'delete', id }) })).json();
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
    html += `<span style="color:var(--text-dim)"> / </span><a onclick="srvBrowseGoIdx('${target.replace(/'/g, '')}');return false">${escHtml(sg)}</a>`;
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
let heatAt = 0;
async function loadHeat() {
  if (Date.now() - heatAt < 60000) return;
  heatAt = Date.now();
  try {
    const d = await (await fetch(U('/api/activity?days=14'))).json();
    const days = (d && d.days) || [];
    const box = document.getElementById('heatMap');
    if (!days.length) { box.innerHTML = ''; return; }
    const mx = Math.max(1, ...days.map(x => x.joins));
    let joins = 0, bks = 0;
    box.innerHTML = days.map(x => {
      joins += x.joins;
      if (x.backups) bks++;
      const a = 0.1 + 0.9 * (x.joins / mx);
      const h = 22 + 52 * (x.joins / mx);
      return `<div class="heat-cell${x.backups ? ' has-bk' : ''}" style="height:${h}px; background:rgba(168,85,247,${a.toFixed(2)})" title="${x.date}: ${x.joins} joins${x.backups ? ` · ${x.backups} backup(s)` : ''}"></div>`;
    }).join('');
    document.getElementById('heatBadge').textContent = `${joins} joins · ${bks} días con backup`;
  } catch (e) {}
}

async function loadProps() {
  try {
    const res = await fetch(U('/api/file?path=server.properties'));
    const data = await res.json();
    document.getElementById('propsText').value = data.content || '';
  } catch (e) {}
}

async function saveProps() {
  const content = document.getElementById('propsText').value;
  try {
    const res = await fetch(U('/api/file'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: 'server.properties', content: content })
    });
    const data = await res.json();
    showToast(data.msg || 'Guardado correctamente');
  } catch (e) {
    showToast('Error al guardar');
  }
}

/* ══════════════════════════════════════════════════
   PLAYER INSPECTOR (Ficha detallada & acciones rápidas)
══════════════════════════════════════════════════ */
let CURRENT_PM_PLAYER = '';
async function openPlayerModal(name) {
  CURRENT_PM_PLAYER = name;
  const modal = document.getElementById('playerModal');
  modal.style.display = 'flex';
  document.getElementById('pmName').textContent = name;
  document.getElementById('pmAvatar').src = 'https://mc-heads.net/body/' + encodeURIComponent(name) + '/120';
  document.getElementById('pmUuid').textContent = 'Consultando...';
  document.getElementById('pmHours').textContent = '…';
  document.getElementById('pmDeaths').textContent = '…';
  document.getElementById('pmMobKills').textContent = '…';
  document.getElementById('pmDist').textContent = '…';
  document.getElementById('pmCoords').textContent = '…';
  document.getElementById('pmLevel').textContent = '…';

  try {
    const r = await (await fetch(U('/api/player-info?name=' + encodeURIComponent(name)))).json();
    if (r.ok) {
      document.getElementById('pmUuid').textContent = r.uuid || 'Sin UUID registrado';
      document.getElementById('pmHours').textContent = r.hours_played != null ? r.hours_played + ' h' : '—';
      document.getElementById('pmDeaths').textContent = r.deaths != null ? r.deaths : '0';
      document.getElementById('pmMobKills').textContent = r.mob_kills != null ? r.mob_kills.toLocaleString() : '0';
      document.getElementById('pmDist').textContent = r.km_traveled != null ? r.km_traveled + ' km' : '—';
      document.getElementById('pmCoords').textContent = r.coords ? `X:${r.coords[0]} Y:${r.coords[1]} Z:${r.coords[2]}` : (r.level != null ? 'En línea' : 'Desconectado');
      document.getElementById('pmLevel').textContent = r.level != null ? r.level : '—';
    }
  } catch (e) {
    document.getElementById('pmUuid').textContent = 'Error al cargar estadísticas';
  }
}
function closePlayerModal() {
  document.getElementById('playerModal').style.display = 'none';
}
async function pmQuickCmd(act) {
  if (!CURRENT_PM_PLAYER) return;
  const p = CURRENT_PM_PLAYER;
  let cmd = '';
  if (act === 'heal') cmd = `effect give ${p} instant_health 1 255`;
  else if (act === 'feed') cmd = `effect give ${p} saturation 1 255`;
  else if (act === 'creative') cmd = `gamemode creative ${p}`;
  else if (act === 'survival') cmd = `gamemode survival ${p}`;
  else if (act === 'spawn') cmd = `spawnpoint ${p}`;
  else if (act === 'clearlag') {
    if (!confirm(`¿Vaciar todo el inventario de "${p}"?`)) return;
    cmd = `clear ${p}`;
  }
  if (!cmd) return;
  try {
    const r = await (await fetch(U('/api/cmd'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cmd }) })).json();
    showToast(r.msg || 'Comando enviado');
    addNotif(`Comando sobre ${p}: ${act}`, 'Ahora mismo', 'zap');
  } catch (e) {
    showToast('Error al enviar acción', 'error');
  }
}

/* ══════════════════════════════════════════════════
   MONITOR DE RED Y LATENCIA
══════════════════════════════════════════════════ */
async function refreshPingMetrics() {
  const elMc = document.getElementById('pingLocalMc');
  const elRcon = document.getElementById('pingLocalRcon');
  const elTun = document.getElementById('pingTunnel');
  if (elMc) elMc.textContent = '…';
  if (elRcon) elRcon.textContent = '…';
  if (elTun) elTun.textContent = '…';

  try {
    const r = await (await fetch(U('/api/network-ping'))).json();
    const paintPing = (el, subEl, ms, labelDefault) => {
      if (!el) return;
      if (ms == null) {
        el.textContent = 'Inactivo';
        el.style.color = 'var(--text-dim)';
      } else {
        el.textContent = ms + ' ms';
        el.style.color = ms < 15 ? '#34d399' : (ms < 60 ? '#fbbf24' : '#f87171');
      }
    };
    paintPing(elMc, document.getElementById('pingLocalMcSub'), r.local_mc_ms);
    paintPing(elRcon, document.getElementById('pingLocalRconSub'), r.local_rcon_ms);
    paintPing(elTun, document.getElementById('pingTunnelSub'), r.tunnel_ms);
    if (r.public_target) {
      const sub = document.getElementById('pingTunnelSub');
      if (sub) sub.textContent = r.public_target;
    }
  } catch (e) {}
}

/* ══════════════════════════════════════════════════
   SPARK PROFILER
══════════════════════════════════════════════════ */
async function sparkAction(act) {
  const box = document.getElementById('sparkResultBox');
  box.style.display = 'block';
  box.textContent = 'Ejecutando diagnóstico spark en el servidor (' + act + ')...';
  try {
    const r = await (await fetch(U('/api/spark'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: act }) })).json();
    box.textContent = r.msg || 'Completado';
    if (r.msg && r.msg.includes('https://spark.lucko.me/')) {
      const idx = r.msg.indexOf('https://spark.lucko.me/');
      const link = idx >= 0 ? r.msg.slice(idx).split(' ')[0].split(String.fromCharCode(10))[0] : '';
      if (link) {
        showToast('Reporte spark generado: ' + link, 'success');
        addNotif('Reporte Spark disponible: ' + link, 'Ahora', 'activity');
      }
    } else {
      showToast(r.ok ? 'Spark OK' : (r.msg || 'Error'), r.ok ? 'success' : 'error');
    }
  } catch (e) {
    box.textContent = 'Error al comunicar con spark.';
  }
}
function sparkQuickAction(act) {
  switchTab('metrics');
  setTimeout(() => sparkAction(act), 400);
}

/* ══════════════════════════════════════════════════
   MODRINTH EXPLORER (Buscador & Instalador 1-click)
══════════════════════════════════════════════════ */
function toggleModrinthView() {
  const s = document.getElementById('modrinthSection');
  s.style.display = s.style.display === 'none' ? 'block' : 'none';
  if (s.style.display === 'block') {
    const inp = document.getElementById('mrSearchInput');
    inp.focus();
    if (!document.getElementById('mrResults').dataset.loaded) {
      searchModrinth();
    }
  }
}
async function searchModrinth() {
  const q = document.getElementById('mrSearchInput').value.trim();
  const box = document.getElementById('mrResults');
  box.dataset.loaded = '1';
  box.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:12px">Buscando en Modrinth para NeoForge 1.21.1...</div>';
  try {
    const r = await (await fetch(U('/api/modrinth/search?q=' + encodeURIComponent(q)))).json();
    if (!r.ok || !r.hits || !r.hits.length) {
      box.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:12px">No se encontraron mods para esa búsqueda.</div>';
      return;
    }
    box.innerHTML = r.hits.map(h => {
      const icon = h.icon || '/icon.svg';
      const safeTitle = escHtml(h.title);
      const safeDesc = escHtml(h.description || 'Sin descripción');
      const dlK = h.downloads > 1000000 ? (h.downloads / 1000000).toFixed(1) + 'M' : Math.round(h.downloads / 1000) + 'k';
      return `<div class="mr-card">
        <div style="display:flex; gap:12px; align-items:flex-start">
          <img src="${icon}" class="mr-icon" alt="" onerror="this.src='/icon.svg'">
          <div style="flex:1; min-width:0">
            <div style="font-weight:700; font-size:13.5px; color:#fff; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">${safeTitle}</div>
            <div style="font-size:11px; color:var(--text-dim)">por ${escHtml(h.author || 'Desconocido')} · ${dlK} descargas</div>
          </div>
        </div>
        <p style="font-size:11.5px; color:#cbd5e1; margin:10px 0 14px; line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden">${safeDesc}</p>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span class="mchip ld">NeoForge</span>
          <button class="cmd-btn" style="padding:4px 12px; font-size:11.5px" onclick="installModrinth('${escHtml(h.slug || h.id)}', '${safeTitle.replace(/'/g, "")}')">Instalar</button>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    box.innerHTML = '<div style="color:#f87171;font-size:12px;padding:12px">Error conectando con la API de Modrinth.</div>';
  }
}
async function installModrinth(slug, title) {
  showToast(`Descargando ${title} desde Modrinth...`);
  try {
    const r = await (await fetch(U('/api/modrinth/install'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug, loader: 'neoforge', game_ver: '1.21.1' })
    })).json();
    showToast(r.msg || 'Instalado', r.ok ? 'success' : 'error');
    if (r.ok) {
      addNotif(`Mod instalado: ${title}`, 'Ahora', 'download');
      loadMods();
    }
  } catch (e) {
    showToast('Error al descargar mod', 'error');
  }
}

/* ══════════════════════════════════════════════════
   WORLD MANAGER & MCA REGION CLEANER
══════════════════════════════════════════════════ */
let WORLD_DATA = null;
let MCA_ACTIVE_DIM = 'overworld';
let MCA_MAP_MODE = 'terrain';
let MCA_TILES = {};
let MCA_VIEW = { cx: 0, cz: 0, scale: 14 };
let MCA_SEL = { overworld: new Set(), nether: new Set(), end: new Set() };
let MCA_HOVER = null;
const CHUNK_ZOOM = 320;
let MCA_CHUNK_SEL = { overworld: new Set(), nether: new Set(), end: new Set() };
let MCA_DETAIL_CACHE = {};
let MCA_HOVER_CHUNK = null;
let MAP_NEED_DETAILS = [];
let MCA_MARQUEE = null;
let MCA_DETAIL_FILE = null;
async function loadWorldManager() {
  const container = document.getElementById('mcaListContainer');
  if (container) container.innerHTML = '<div class="mca-row" style="color:var(--text-dim)">Cargando análisis del mundo...</div>';
  try {
    const d = await (await fetch(U('/api/world'))).json();
    if (!d || !d.exists) {
      if (container) container.innerHTML = '<div class="mca-row">La carpeta del mundo no existe en este servidor.</div>';
      return;
    }
    WORLD_DATA = d;
    const mb = bytes => bytes > 1073741824 ? (bytes / 1073741824).toFixed(2) + ' GB' : (bytes > 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.round(bytes / 1024) + ' KB');
    
    (d.dimensions || []).forEach(dim => {
      if (dim.id === 'overworld') {
        document.getElementById('dimOwSize').textContent = mb(dim.size);
        document.getElementById('dimOwRegions').textContent = dim.regions_count + ' regiones';
      } else if (dim.id === 'nether') {
        document.getElementById('dimNetherSize').textContent = mb(dim.size);
        document.getElementById('dimNetherRegions').textContent = dim.regions_count + ' regiones';
      } else if (dim.id === 'end') {
        document.getElementById('dimEndSize').textContent = mb(dim.size);
        document.getElementById('dimEndRegions').textContent = dim.regions_count + ' regiones';
      }
    });

    renderMcaList();
    initWorldMap();
    drawWorldMap();
  } catch (e) {
    if (container) container.innerHTML = '<div class="mca-row">Error al cargar datos del mundo.</div>';
  }
}
function selectMcaDim(dim) {
  MCA_ACTIVE_DIM = dim;
  document.querySelectorAll('#mcaDimSeg button').forEach(b => {
    b.classList.toggle('on', b.dataset.dim === dim || b.textContent.toLowerCase().includes(dim === 'overworld' ? 'over' : dim === 'nether' ? 'nether' : 'end'));
  });
  closeRegionDetail();
  renderMcaList();
  drawWorldMap();
}
function renderMcaList() {
  const container = document.getElementById('mcaListContainer');
  if (!container || !WORLD_DATA) return;
  const dim = (WORLD_DATA.dimensions || []).find(d => d.id === MCA_ACTIVE_DIM);
  if (!dim || !dim.regions || !dim.regions.length) {
    container.innerHTML = '<div class="mca-row" style="color:var(--text-dim)">Sin archivos de región en esta dimensión.</div>';
    document.getElementById('mcaSelectedCount').textContent = '0 regiones seleccionadas';
    return;
  }
  const mb = bytes => bytes > 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.round(bytes / 1024) + ' KB';
  const sel = MCA_SEL[MCA_ACTIVE_DIM] || (MCA_SEL[MCA_ACTIVE_DIM] = new Set());
  container.innerHTML = dim.regions.map((r, i) => {
    const x0 = r.x * 512, x1 = (r.x + 1) * 512 - 1;
    const z0 = r.z * 512, z1 = (r.z + 1) * 512 - 1;
    const pct = Math.round(((r.present || 0) / 1024) * 100);
    return `<div class="mca-row">
      <input type="checkbox" class="mca-chk" data-file="${r.file}"${sel.has(r.file) ? ' checked' : ''} onchange="mcaChkToggle(this)">
      <span style="color:#e2e8f0; font-weight:700; width:130px">${r.file}</span>
      <span style="color:var(--text-dim); flex:1">Bloques: [X ${x0}..${x1}, Z ${z0}..${z1}] · ${r.present || 0}/1024 chunks (${pct}%)</span>
      <span style="color:#93c5fd">${mb(r.size)}</span>
    </div>`;
  }).join('');
  updateMcaSelectionCount();
}
function mcaChkToggle(el) {
  const sel = MCA_SEL[MCA_ACTIVE_DIM] || (MCA_SEL[MCA_ACTIVE_DIM] = new Set());
  if (el.checked) sel.add(el.dataset.file);
  else sel.delete(el.dataset.file);
  updateMcaSelectionCount();
  drawWorldMap();
}
function updateMcaSelectionCount() {
  const sel = MCA_SEL[MCA_ACTIVE_DIM] || new Set();
  const csel = MCA_CHUNK_SEL[MCA_ACTIVE_DIM] || new Set();
  const checked = document.querySelectorAll('.mca-chk:checked').length;
  const n = Math.max(sel.size, checked);
  const el = document.getElementById('mcaSelectedCount');
  if (csel.size) el.textContent = `${n} regiones + ${csel.size} chunks sueltos seleccionados`;
  else el.textContent = `${n} regiones seleccionadas (${n * 1024} chunks)`;
  const btn = document.getElementById('mcaDeleteBtn');
  if (btn) btn.textContent = (n || csel.size) ? `Borrar selección (${n} reg. + ${csel.size} chunks)` : 'Borrar regiones seleccionadas';
  try { updateRegionPreview(); } catch (e) {}
}
function toggleSelectAllMca(check) {
  const dim = (WORLD_DATA.dimensions || []).find(d => d.id === MCA_ACTIVE_DIM);
  const sel = MCA_SEL[MCA_ACTIVE_DIM] || (MCA_SEL[MCA_ACTIVE_DIM] = new Set());
  if (check && dim && dim.regions) dim.regions.forEach(r => sel.add(r.file));
  else sel.clear();
  document.querySelectorAll('.mca-chk').forEach(c => c.checked = check);
  updateMcaSelectionCount();
  drawWorldMap();
}
/* ── Motor del visor de mapa ── */
function mapRegions() {
  if (!WORLD_DATA) return [];
  const dim = (WORLD_DATA.dimensions || []).find(d => d.id === MCA_ACTIVE_DIM);
  return (dim && dim.regions) || [];
}
function setMapMode(mode) {
  MCA_MAP_MODE = mode;
  document.querySelectorAll('#mapModeSeg button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  drawWorldMap();
}
function mapZoom(f) {
  MCA_VIEW.scale = Math.max(3, Math.min(1024, MCA_VIEW.scale * f));
  drawWorldMap();
}
function mapResetView() {
  MCA_VIEW.cx = 0; MCA_VIEW.cz = 0; MCA_VIEW.scale = 14;
  drawWorldMap();
}
function mapClearSel() {
  (MCA_SEL[MCA_ACTIVE_DIM] || new Set()).clear();
  (MCA_CHUNK_SEL[MCA_ACTIVE_DIM] || new Set()).clear();
  renderMcaList();
  drawWorldMap();
}
function toggleMapFullscreen() {
  const card = document.getElementById('mcaMapCard');
  if (!card) return;
  card.classList.toggle('map-full');
  if (card.classList.contains('map-full')) card.scrollIntoView({ block: 'start' });
  requestAnimationFrame(() => setTimeout(drawWorldMap, 60));
}
window.addEventListener('resize', () => {
  const t = document.getElementById('tab-worlds');
  if (t && t.classList.contains('active')) drawWorldMap();
});
function mapRegionColor(r, tmin, tmax) {
  if (!(r.present > 0)) return { fill: 'rgba(148,163,184,0.10)', edge: 'rgba(148,163,184,0.25)' };
  if (MCA_MAP_MODE === 'fill') {
    const f = (r.present || 0) / 1024;
    if (f >= 0.9) return { fill: 'rgba(52,211,153,0.85)', edge: 'rgba(52,211,153,0.9)' };
    if (f >= 0.5) return { fill: 'rgba(52,211,153,0.55)', edge: 'rgba(52,211,153,0.7)' };
    if (f >= 0.15) return { fill: 'rgba(251,191,36,0.55)', edge: 'rgba(251,191,36,0.7)' };
    return { fill: 'rgba(251,191,36,0.28)', edge: 'rgba(251,191,36,0.45)' };
  }
  const t = r.mtime || 0;
  if (!t || tmax <= tmin) return { fill: 'rgba(168,85,247,0.5)', edge: 'rgba(168,85,247,0.7)' };
  const k = (t - tmin) / (tmax - tmin);
  if (k > 0.66) return { fill: 'rgba(52,211,153,0.85)', edge: 'rgba(52,211,153,0.95)' };
  if (k > 0.33) return { fill: 'rgba(168,85,247,0.6)', edge: 'rgba(168,85,247,0.8)' };
  return { fill: 'rgba(100,116,139,0.5)', edge: 'rgba(100,116,139,0.7)' };
}
function initWorldMap() {
  const cv = document.getElementById('worldMapCanvas');
  if (!cv || cv.dataset.init === '1') return;
  cv.dataset.init = '1';
  let drag = null, moved = 0;
  const pos = e => {
    const r = cv.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  cv.addEventListener('contextmenu', e => e.preventDefault());
  cv.addEventListener('mousedown', e => {
    const p = pos(e);
    if (e.shiftKey || e.button === 2) {
      MCA_MARQUEE = { x0: p.x, y0: p.y, x1: p.x, y1: p.y, sub: (e.button === 2) || e.altKey };
      drag = null; moved = 0;
      drawWorldMap();
      return;
    }
    drag = p; moved = 0;
  });
  window.addEventListener('mouseup', () => {
    if (MCA_MARQUEE) {
      const m = MCA_MARQUEE;
      MCA_MARQUEE = null;
      if (Math.abs(m.x1 - m.x0) > 6 || Math.abs(m.y1 - m.y0) > 6) marqueeSelect(m);
      else drawWorldMap();
    }
    drag = null;
  });
  cv.addEventListener('mousemove', e => {
    const p = pos(e);
    if (MCA_MARQUEE) {
      MCA_MARQUEE.x1 = p.x; MCA_MARQUEE.y1 = p.y;
      drawWorldMap();
      return;
    }
    if (drag) {
      const dx = (p.x - drag.x) / MCA_VIEW.scale, dy = (p.y - drag.y) / MCA_VIEW.scale;
      moved += Math.abs(p.x - drag.x) + Math.abs(p.y - drag.y);
      MCA_VIEW.cx -= dx; MCA_VIEW.cz -= dy;
      drag = p;
      drawWorldMap();
      return;
    }
    MCA_HOVER = mapPick(p.x, p.y);
    MCA_HOVER_CHUNK = mapPickChunk(p.x, p.y, MCA_HOVER);
    drawWorldMap();
    const tip = document.getElementById('worldMapTip');
    if (MCA_HOVER && tip) {
      const r = MCA_HOVER;
      const dt = r.mtime ? new Date(r.mtime * 1000).toLocaleDateString() : 'sin fecha';
      tip.style.display = 'block';
      tip.style.left = (p.x + 14) + 'px';
      tip.style.top = (p.y + 14) + 'px';
      let txt = r.file + ' · R(' + r.x + ',' + r.z + ') · ' + (r.present || 0) + '/1024 chunks · ' + dt;
      if (MCA_HOVER_CHUNK) {
        const bx0 = (r.x * 32 + MCA_HOVER_CHUNK.cx) * 16, bx1 = bx0 + 15;
        const bz0 = (r.z * 32 + MCA_HOVER_CHUNK.cz) * 16, bz1 = bz0 + 15;
        txt += ' · chunk (' + MCA_HOVER_CHUNK.cx + ',' + MCA_HOVER_CHUNK.cz + ') X ' + bx0 + '..' + bx1 + ' Z ' + bz0 + '..' + bz1;
      }
      tip.textContent = txt;
    } else if (tip) tip.style.display = 'none';
  });
  cv.addEventListener('mouseleave', () => {
    MCA_HOVER = null;
    MCA_HOVER_CHUNK = null;
    const tip = document.getElementById('worldMapTip');
    if (tip) tip.style.display = 'none';
    drawWorldMap();
  });
  cv.addEventListener('click', e => {
    if (moved > 6) { moved = 0; return; }
    moved = 0;
    const p = pos(e);
    const r = mapPick(p.x, p.y);
    if (!r) return;
    if (MCA_VIEW.scale >= CHUNK_ZOOM) {
      const ch = mapPickChunk(p.x, p.y, r);
      if (!ch) return;
      const csel = MCA_CHUNK_SEL[MCA_ACTIVE_DIM] || (MCA_CHUNK_SEL[MCA_ACTIVE_DIM] = new Set());
      const ck = r.file + ':' + ch.cx + ':' + ch.cz;
      if (csel.has(ck)) csel.delete(ck);
      else csel.add(ck);
      updateMcaSelectionCount();
      drawWorldMap();
      return;
    }
    const sel = MCA_SEL[MCA_ACTIVE_DIM] || (MCA_SEL[MCA_ACTIVE_DIM] = new Set());
    if (sel.has(r.file)) sel.delete(r.file);
    else sel.add(r.file);
    renderMcaList();
    drawWorldMap();
    showRegionDetail(r.file);
  });
  cv.addEventListener('wheel', e => {
    e.preventDefault();
    mapZoom(e.deltaY < 0 ? 1.15 : 0.87);
  }, { passive: false });
  cv.addEventListener('dblclick', e => {
    const r = cv.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const hit = mapPick(px, py);
    if (hit) { MCA_VIEW.cx = hit.x; MCA_VIEW.cz = hit.z; }
    else {
      MCA_VIEW.cx += (px - r.width / 2) / MCA_VIEW.scale;
      MCA_VIEW.cz += (py - r.height / 2) / MCA_VIEW.scale;
    }
    MCA_VIEW.scale = Math.min(1024, MCA_VIEW.scale * 1.6);
    drawWorldMap();
  });
}
function mapPick(px, py) {
  const cv = document.getElementById('worldMapCanvas');
  if (!cv) return null;
  const w = cv.clientWidth, h = cv.clientHeight, s = MCA_VIEW.scale;
  let best = null, bestD = 1e9;
  for (const r of mapRegions()) {
    const rx = (r.x - MCA_VIEW.cx) * s + w / 2, ry = (r.z - MCA_VIEW.cz) * s + h / 2;
    const dx = px - (rx + s / 2), dy = py - (ry + s / 2);
    const d = Math.max(Math.abs(dx), Math.abs(dy));
    if (px >= rx && px <= rx + s && py >= ry && py <= ry + s && d < bestD) { best = r; bestD = d; }
  }
  return best;
}
function mapPickChunk(px, py, r) {
  if (!r || MCA_VIEW.scale < CHUNK_ZOOM) return null;
  const cv = document.getElementById('worldMapCanvas');
  if (!cv) return null;
  const w = cv.clientWidth, h = cv.clientHeight, s = MCA_VIEW.scale;
  const x = (r.x - MCA_VIEW.cx) * s + w / 2, y = (r.z - MCA_VIEW.cz) * s + h / 2;
  const cx = Math.floor((px - x) / s * 32), cz = Math.floor((py - y) / s * 32);
  if (cx < 0 || cx > 31 || cz < 0 || cz > 31) return null;
  return { cx, cz };
}
function marqueeSelect(m) {
  const cv = document.getElementById('worldMapCanvas');
  if (!cv) return;
  const w = cv.clientWidth, h = cv.clientHeight, s = MCA_VIEW.scale;
  const wx0 = (Math.min(m.x0, m.x1) - w / 2) / s + MCA_VIEW.cx;
  const wx1 = (Math.max(m.x0, m.x1) - w / 2) / s + MCA_VIEW.cx;
  const wz0 = (Math.min(m.y0, m.y1) - h / 2) / s + MCA_VIEW.cz;
  const wz1 = (Math.max(m.y0, m.y1) - h / 2) / s + MCA_VIEW.cz;
  const sub = !!m.sub;
  const regs = mapRegions();
  if (s >= CHUNK_ZOOM) {
    const csel = MCA_CHUNK_SEL[MCA_ACTIVE_DIM] || (MCA_CHUNK_SEL[MCA_ACTIVE_DIM] = new Set());
    let n = 0;
    for (const r of regs) {
      if (r.x + 1 < wx0 || r.x > wx1 || r.z + 1 < wz0 || r.z > wz1) continue;
      if (!(r.present > 0)) continue;
      const cx0 = Math.max(0, Math.floor((wx0 - r.x) * 32)), cx1 = Math.min(31, Math.floor((wx1 - r.x) * 32));
      const cz0 = Math.max(0, Math.floor((wz0 - r.z) * 32)), cz1 = Math.min(31, Math.floor((wz1 - r.z) * 32));
      for (let cz = cz0; cz <= cz1; cz++) {
        for (let cx = cx0; cx <= cx1; cx++) {
          const ck = r.file + ':' + cx + ':' + cz;
          if (sub) { if (csel.delete(ck)) n++; }
          else if (!csel.has(ck)) { csel.add(ck); n++; }
        }
      }
    }
    showToast(sub ? (n + ' chunks quitados de la selección') : (n + ' chunks añadidos a la selección'), 'success');
  } else {
    const sel = MCA_SEL[MCA_ACTIVE_DIM] || (MCA_SEL[MCA_ACTIVE_DIM] = new Set());
    let n = 0;
    for (const r of regs) {
      if (r.x + 1 < wx0 || r.x > wx1 || r.z + 1 < wz0 || r.z > wz1) continue;
      if (sub) { if (sel.delete(r.file)) n++; }
      else if (!sel.has(r.file)) { sel.add(r.file); n++; }
    }
    renderMcaList();
    showToast(sub ? (n + ' regiones quitadas de la selección') : (n + ' regiones añadidas a la selección'), 'success');
  }
  updateMcaSelectionCount();
  drawWorldMap();
  updateRegionPreview();
}
function mapTileVersion(rx, rz) {
  try {
    const dim = (WORLD_DATA.dimensions || []).find(d => d.id === MCA_ACTIVE_DIM);
    const r = dim && dim.regions ? dim.regions.find(q => q.x === rx && q.z === rz) : null;
    return (r && r.mtime) || 0;
  } catch (e) { return 0; }
}
function mapQueueTile(key, rx, rz) {
  if (MCA_TILES[key]) return;
  const dim = MCA_ACTIVE_DIM;
  const img = new Image();
  MCA_TILES[key] = { img, ok: false, loading: true };
  img.onload = () => { MCA_TILES[key].ok = true; MCA_TILES[key].loading = false; drawWorldMap(); try { updateRegionPreview(); } catch (e) {} };
  img.onerror = () => { MCA_TILES[key].ok = false; MCA_TILES[key].loading = false; };
  const v = mapTileVersion(rx, rz);
  img.src = U('/api/world/tile?dimension=' + encodeURIComponent(dim) + '&rx=' + rx + '&rz=' + rz + (v ? '&v=' + v : ''));
}
function mapInvalidateFiles(files) {
  (files || []).forEach(fn => {
    const m = /^r\.(-?\d+)\.(-?\d+)\.mca$/.exec(fn);
    if (!m) return;
    delete MCA_TILES[MCA_ACTIVE_DIM + ':' + m[1] + ':' + m[2]];
    delete MCA_DETAIL_CACHE[MCA_ACTIVE_DIM + ':' + fn];
  });
}
function mapInvalidateDim(dim) {
  Object.keys(MCA_TILES).forEach(k => { if (k.indexOf(dim + ':') === 0) delete MCA_TILES[k]; });
  Object.keys(MCA_DETAIL_CACHE).forEach(k => { if (k.indexOf(dim + ':') === 0) delete MCA_DETAIL_CACHE[k]; });
  MCA_SEL[dim] = new Set();
  MCA_CHUNK_SEL[dim] = new Set();
}
async function mapRefreshDetail() {
  if (!MCA_DETAIL_FILE) return;
  try {
    const dim = (WORLD_DATA.dimensions || []).find(d => d.id === MCA_ACTIVE_DIM);
    const still = dim && dim.regions ? dim.regions.some(r => r.file === MCA_DETAIL_FILE) : false;
    if (still) await showRegionDetail(MCA_DETAIL_FILE);
    else closeRegionDetail();
  } catch (e) {}
}
function mapQueueDetail(key, file) {
  if (MCA_DETAIL_CACHE[key]) return;
  MCA_DETAIL_CACHE[key] = { loading: true, grid: null };
  fetch(U('/api/world/region?dimension=' + encodeURIComponent(MCA_ACTIVE_DIM) + '&file=' + encodeURIComponent(file)))
    .then(r => r.json())
    .then(d => {
      MCA_DETAIL_CACHE[key] = { loading: false, grid: (d && d.ok && d.grid) || null };
      drawWorldMap();
    })
    .catch(() => { MCA_DETAIL_CACHE[key] = { loading: false, grid: null }; });
}
function drawChunkOverlay(ctx, r, x, y, s) {
  const key = MCA_ACTIVE_DIM + ':' + r.file;
  const det = MCA_DETAIL_CACHE[key];
  const chunkSel = MCA_CHUNK_SEL[MCA_ACTIVE_DIM] || new Set();
  const cs = s / 32;
  if (!det || !det.grid) {
    if (!det) MAP_NEED_DETAILS.push({ key, file: r.file, d: (r.x - MCA_VIEW.cx) * (r.x - MCA_VIEW.cx) + (r.z - MCA_VIEW.cz) * (r.z - MCA_VIEW.cz) });
    ctx.strokeStyle = 'rgba(255,255,255,0.10)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < 32; i++) {
      ctx.moveTo(x + i * cs, y); ctx.lineTo(x + i * cs, y + s);
      ctx.moveTo(x, y + i * cs); ctx.lineTo(x + s, y + i * cs);
    }
    ctx.stroke();
    return;
  }
  for (let cz = 0; cz < 32; cz++) {
    for (let cx = 0; cx < 32; cx++) {
      const gen = det.grid[cz * 32 + cx];
      const ck = r.file + ':' + cx + ':' + cz;
      if (chunkSel.has(ck)) {
        ctx.fillStyle = 'rgba(168,85,247,0.55)';
        ctx.fillRect(x + cx * cs + 0.5, y + cz * cs + 0.5, cs - 1, cs - 1);
      } else if (!gen) {
        ctx.fillStyle = 'rgba(0,0,0,0.35)';
        ctx.fillRect(x + cx * cs + 0.5, y + cz * cs + 0.5, cs - 1, cs - 1);
      }
      if (MCA_HOVER_CHUNK && MCA_HOVER_CHUNK.file === r.file && MCA_HOVER_CHUNK.cx === cx && MCA_HOVER_CHUNK.cz === cz) {
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 1.5;
        ctx.strokeRect(x + cx * cs + 1, y + cz * cs + 1, cs - 2, cs - 2);
      }
    }
  }
  ctx.strokeStyle = 'rgba(255,255,255,0.12)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 1; i < 32; i++) {
    ctx.moveTo(x + i * cs, y); ctx.lineTo(x + i * cs, y + s);
    ctx.moveTo(x, y + i * cs); ctx.lineTo(x + s, y + i * cs);
  }
  ctx.stroke();
}
function drawWorldMap() {
  const cv = document.getElementById('worldMapCanvas');
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || cv.parentElement.clientWidth, h = cv.clientHeight || 420;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const regs = mapRegions();
  const sel = MCA_SEL[MCA_ACTIVE_DIM] || new Set();
  const times = regs.filter(r => r.mtime > 0).map(r => r.mtime);
  const tmin = times.length ? Math.min.apply(null, times) : 0;
  const tmax = times.length ? Math.max.apply(null, times) : 0;
  const s = MCA_VIEW.scale;
  ctx.strokeStyle = 'rgba(168,85,247,0.25)';
  ctx.lineWidth = 1;
  const ox = (0 - MCA_VIEW.cx) * s + w / 2, oy = (0 - MCA_VIEW.cz) * s + h / 2;
  ctx.beginPath(); ctx.moveTo(ox, 0); ctx.lineTo(ox, h); ctx.moveTo(0, oy); ctx.lineTo(w, oy); ctx.stroke();
  ctx.fillStyle = 'rgba(192,132,252,0.9)';
  ctx.font = '10px JetBrains Mono, monospace';
  ctx.fillText('0,0 spawn', ox + 5, oy - 5);
  const terrain = MCA_MAP_MODE === 'terrain';
  const pendingTiles = [];
  MAP_NEED_DETAILS = [];
  const chunkSel = MCA_CHUNK_SEL[MCA_ACTIVE_DIM] || new Set();
  for (const r of regs) {
    const x = (r.x - MCA_VIEW.cx) * s + w / 2, y = (r.z - MCA_VIEW.cz) * s + h / 2;
    if (x + s < -20 || y + s < -20 || x > w + 20 || y > h + 20) continue;
    const pad = s > 10 ? 1 : 0;
    if (terrain && (r.present > 0)) {
      const key = MCA_ACTIVE_DIM + ':' + r.x + ':' + r.z;
      const t = MCA_TILES[key];
      if (t && t.img && t.ok) {
        ctx.drawImage(t.img, x, y, s, s);
      } else {
        ctx.fillStyle = 'rgba(148,163,184,0.10)';
        ctx.fillRect(x + pad / 2, y + pad / 2, s - pad, s - pad);
        if (!t) pendingTiles.push({ key, rx: r.x, rz: r.z, d: (r.x - MCA_VIEW.cx) * (r.x - MCA_VIEW.cx) + (r.z - MCA_VIEW.cz) * (r.z - MCA_VIEW.cz) });
      }
    } else {
      const c = mapRegionColor(r, tmin, tmax);
      ctx.fillStyle = c.fill;
      ctx.fillRect(x + pad / 2, y + pad / 2, s - pad, s - pad);
    }
    if (sel.has(r.file)) {
      ctx.save();
      ctx.shadowColor = 'rgba(168,85,247,0.9)';
      ctx.shadowBlur = 8;
      ctx.strokeStyle = '#c084fc';
      ctx.lineWidth = 2;
      ctx.strokeRect(x + 1, y + 1, s - 2, s - 2);
      ctx.restore();
    } else if (MCA_HOVER && MCA_HOVER.file === r.file) {
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x + 0.5, y + 0.5, s - 1, s - 1);
    } else {
      ctx.strokeStyle = terrain ? 'rgba(255,255,255,0.14)' : mapRegionColor(r, tmin, tmax).edge;
      ctx.lineWidth = 1;
      ctx.strokeRect(x + 0.5, y + 0.5, s - 1, s - 1);
    }
    if (s >= CHUNK_ZOOM && (r.present > 0)) {
      drawChunkOverlay(ctx, r, x, y, s);
    }
  }
  if (MCA_MARQUEE) {
    const mx = Math.min(MCA_MARQUEE.x0, MCA_MARQUEE.x1), my = Math.min(MCA_MARQUEE.y0, MCA_MARQUEE.y1);
    const mw = Math.abs(MCA_MARQUEE.x1 - MCA_MARQUEE.x0), mh = Math.abs(MCA_MARQUEE.y1 - MCA_MARQUEE.y0);
    ctx.fillStyle = MCA_MARQUEE.sub ? 'rgba(248,113,113,0.12)' : 'rgba(168,85,247,0.15)';
    ctx.fillRect(mx, my, mw, mh);
    ctx.strokeStyle = MCA_MARQUEE.sub ? '#f87171' : '#c084fc';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(mx + 0.5, my + 0.5, mw - 1, mh - 1);
    ctx.setLineDash([]);
  }
  if (pendingTiles.length) {
    pendingTiles.sort((a, b) => a.d - b.d);
    pendingTiles.slice(0, 60).forEach(t => mapQueueTile(t.key, t.rx, t.rz));
  }
  if (MAP_NEED_DETAILS.length) {
    MAP_NEED_DETAILS.sort((a, b) => a.d - b.d);
    MAP_NEED_DETAILS.slice(0, 24).forEach(t => mapQueueDetail(t.key, t.file));
  }
  const lg = document.getElementById('mapLegend');
  if (lg) lg.innerHTML = MCA_MAP_MODE === 'terrain'
    ? '<span><span class="lm-sw" style="background:#6faf47"></span>Tierra</span><span><span class="lm-sw" style="background:#2f6ed6"></span>Agua</span><span><span class="lm-sw" style="background:#f0f6fa"></span>Nieve</span><span><span class="lm-sw" style="background:rgba(148,163,184,0.2)"></span>Vacía</span>'
    : MCA_MAP_MODE === 'fill'
    ? '<span><span class="lm-sw" style="background:rgba(52,211,153,0.85)"></span>Llena (90%+)</span><span><span class="lm-sw" style="background:rgba(52,211,153,0.5)"></span>Media</span><span><span class="lm-sw" style="background:rgba(251,191,36,0.55)"></span>Escasa</span><span><span class="lm-sw" style="background:rgba(148,163,184,0.2)"></span>Vacía</span>'
    : '<span><span class="lm-sw" style="background:rgba(52,211,153,0.85)"></span>Reciente</span><span><span class="lm-sw" style="background:rgba(168,85,247,0.6)"></span>Intermedia</span><span><span class="lm-sw" style="background:rgba(100,116,139,0.5)"></span>Antigua</span><span><span class="lm-sw" style="background:rgba(148,163,184,0.2)"></span>Vacía</span>';
  const hint = document.getElementById('mapHint');
  const nChunks = (MCA_CHUNK_SEL[MCA_ACTIVE_DIM] || new Set()).size;
  if (hint) hint.textContent = regs.length + ' regiones · ' + sel.size + ' reg. + ' + nChunks + ' chunks seleccionados · Arrastra para mover · Shift+arrastrar selecciona en área (Alt o botón derecho quita) · rueda o doble clic para zoom';
}
async function showRegionDetail(file) {
  const box = document.getElementById('regionDetail');
  const title = document.getElementById('regionDetailTitle');
  const meta = document.getElementById('regionDetailMeta');
  const grid = document.getElementById('regionChunks');
  if (!box) return;
  MCA_DETAIL_FILE = file;
  box.style.display = 'block';
  if (title) title.textContent = file + ' (cargando chunks…)';
  if (grid) grid.innerHTML = '';
  updateRegionPreview();
  try {
    const r = await (await fetch(U('/api/world/region?dimension=' + encodeURIComponent(MCA_ACTIVE_DIM) + '&file=' + encodeURIComponent(file)))).json();
    if (!r || !r.ok) {
      if (title) title.textContent = file + ' (sin datos de chunks)';
      return;
    }
    MCA_DETAIL_CACHE[MCA_ACTIVE_DIM + ':' + file] = { loading: false, grid: r.grid };
    drawWorldMap();
    const x0 = r.x * 512, x1 = (r.x + 1) * 512 - 1;
    const z0 = r.z * 512, z1 = (r.z + 1) * 512 - 1;
    if (title) title.textContent = r.file + ' · R(' + r.x + ',' + r.z + ')';
    if (meta) meta.textContent = r.present + '/1024 chunks · bloques X ' + x0 + '..' + x1 + ' · Z ' + z0 + '..' + z1;
    if (grid) grid.innerHTML = r.grid.map(v => `<span class="chk ${v ? 'full' : 'empty'}" title="${v ? 'Chunk generado' : 'Sin generar'}"></span>`).join('');
    updateRegionPreview();
  } catch (e) {
    if (title) title.textContent = file + ' (error al cargar)';
  }
}
function closeRegionDetail() {
  const box = document.getElementById('regionDetail');
  if (box) box.style.display = 'none';
  MCA_DETAIL_FILE = null;
}
function updateRegionPreview() {
  const cv = document.getElementById('regionPreview');
  if (!cv || !MCA_DETAIL_FILE) return;
  const box = document.getElementById('regionDetail');
  if (!box || box.style.display === 'none') return;
  const m = /^r\.(-?\d+)\.(-?\d+)\.mca$/.exec(MCA_DETAIL_FILE);
  if (!m) return;
  const rx = parseInt(m[1], 10), rz = parseInt(m[2], 10);
  const size = 512;
  if (cv.width !== size) { cv.width = size; cv.height = size; }
  const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, size, size);
  ctx.fillStyle = '#06060b';
  ctx.fillRect(0, 0, size, size);
  const key = MCA_ACTIVE_DIM + ':' + MCA_DETAIL_FILE;
  const tkey = MCA_ACTIVE_DIM + ':' + rx + ':' + rz;
  const drawOverlay = () => {
    const csel = MCA_CHUNK_SEL[MCA_ACTIVE_DIM] || new Set();
    const cs = size / 32;
    let anySel = false;
    for (let cz = 0; cz < 32; cz++) {
      for (let cx = 0; cx < 32; cx++) {
        if (csel.has(MCA_DETAIL_FILE + ':' + cx + ':' + cz)) {
          anySel = true;
          ctx.fillStyle = 'rgba(168,85,247,0.55)';
          ctx.fillRect(cx * cs + 0.5, cz * cs + 0.5, cs - 1, cs - 1);
        }
      }
    }
    ctx.strokeStyle = 'rgba(255,255,255,0.12)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < 32; i++) {
      ctx.moveTo(i * cs, 0); ctx.lineTo(i * cs, size);
      ctx.moveTo(0, i * cs); ctx.lineTo(size, i * cs);
    }
    ctx.stroke();
    if (!anySel) {
      ctx.fillStyle = 'rgba(233,213,255,0.75)';
      ctx.font = '12px JetBrains Mono, monospace';
      ctx.fillText('Sin chunks en selección: haz zoom y clic en el mapa', 12, size - 12);
    }
  };
  const t = MCA_TILES[tkey];
  const paintTile = img => {
    try { ctx.drawImage(img, 0, 0, size, size); } catch (e) {}
    drawOverlay();
  };
  if (t && t.img && t.ok) {
    paintTile(t.img);
    return;
  }
  const det = MCA_DETAIL_CACHE[key];
  if (det && det.grid) {
    const cs = size / 32;
    for (let cz = 0; cz < 32; cz++) {
      for (let cx = 0; cx < 32; cx++) {
        ctx.fillStyle = det.grid[cz * 32 + cx] ? '#34d399' : 'rgba(255,255,255,0.05)';
        ctx.fillRect(cx * cs + 0.5, cz * cs + 0.5, cs - 1, cs - 1);
      }
    }
    drawOverlay();
    return;
  }
  ctx.fillStyle = 'rgba(148,163,184,0.5)';
  ctx.font = '13px JetBrains Mono, monospace';
  ctx.fillText('Cargando vista previa…', 12, 24);
  if (!t) mapQueueTile(tkey, rx, rz);
  if (!det) mapQueueDetail(key, MCA_DETAIL_FILE);
  const iv = setInterval(() => {
    const tt = MCA_TILES[tkey], dd = MCA_DETAIL_CACHE[key];
    if ((tt && tt.img && tt.ok) || (dd && dd.grid)) {
      clearInterval(iv);
      updateRegionPreview();
    }
  }, 500);
  setTimeout(() => clearInterval(iv), 15000);
}
function calcMcaFromCoords() {
  const x = parseInt(document.getElementById('calcCoordX').value, 10);
  const z = parseInt(document.getElementById('calcCoordZ').value, 10);
  const res = document.getElementById('calcMcaResult');
  if (isNaN(x) || isNaN(z)) {
    res.textContent = 'Introduce X y Z';
    return;
  }
  const rx = Math.floor(x / 512);
  const rz = Math.floor(z / 512);
  res.textContent = `Región: r.${rx}.${rz}.mca (Chunks ${rx*32}..${rx*32+31}, ${rz*32}..${rz*32+31})`;
}
function selectCalculatedMca() {
  const x = parseInt(document.getElementById('calcCoordX').value, 10);
  const z = parseInt(document.getElementById('calcCoordZ').value, 10);
  if (isNaN(x) || isNaN(z)) return;
  const rx = Math.floor(x / 512);
  const rz = Math.floor(z / 512);
  const targetFn = `r.${rx}.${rz}.mca`;
  const dim = (WORLD_DATA.dimensions || []).find(d => d.id === MCA_ACTIVE_DIM);
  const exists = dim && dim.regions && dim.regions.some(r => r.file === targetFn);
  if (exists) {
    const sel = MCA_SEL[MCA_ACTIVE_DIM] || (MCA_SEL[MCA_ACTIVE_DIM] = new Set());
    sel.add(targetFn);
    MCA_VIEW.cx = rx; MCA_VIEW.cz = rz;
    renderMcaList();
    drawWorldMap();
    showRegionDetail(targetFn);
    showToast(`Región ${targetFn} seleccionada`);
  } else {
    showToast(`El archivo ${targetFn} no existe en ${MCA_ACTIVE_DIM} (chunks no generados aún)`, 'info');
  }
}
async function deleteSelectedMca() {
  const sel = MCA_SEL[MCA_ACTIVE_DIM] || new Set();
  const csel = MCA_CHUNK_SEL[MCA_ACTIVE_DIM] || new Set();
  const files = sel.size ? Array.from(sel) : Array.from(document.querySelectorAll('.mca-chk:checked')).map(c => c.dataset.file);
  const chunks = Array.from(csel).map(k => {
    const p = k.split(':');
    return { file: p[0], cx: parseInt(p[1], 10), cz: parseInt(p[2], 10) };
  });
  if (!files.length && !chunks.length) {
    showToast('Selecciona al menos una región o un chunk (haz zoom para ver chunks)', 'info');
    return;
  }
  const parts = [];
  if (files.length) parts.push(`${files.length} archivos .mca (${files.length * 1024} chunks)`);
  if (chunks.length) parts.push(`${chunks.length} chunks sueltos`);
  const NL = String.fromCharCode(10);
  let warnMsg = `⚠️ ATENCIÓN: ¿Seguro que deseas eliminar ${parts.join(' + ')} de ${MCA_ACTIVE_DIM.toUpperCase()}?` + NL + NL + `Los terrenos se borrarán y se regenerarán limpios la próxima vez que alguien entre.`;
  if (chunks.length) warnMsg += NL + NL + 'NOTA: borrar chunks requiere el servidor DETENIDO.';
  if (!confirm(warnMsg)) return;
  try {
    if (chunks.length) {
      const rc = await (await fetch(U('/api/world/mca'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'delete_chunks', dimension: MCA_ACTIVE_DIM, chunks })
      })).json();
      showToast(rc.msg || 'Chunks eliminados', rc.ok ? 'success' : 'error');
      if (rc.ok) {
        addNotif(`Borrado MCA: ${chunks.length} chunks en ${MCA_ACTIVE_DIM}`, 'Ahora', 'trash');
        MCA_CHUNK_SEL[MCA_ACTIVE_DIM] = new Set();
        mapInvalidateFiles(chunks.map(c => c.file));
      } else {
        return;
      }
    }
    if (files.length) {
      const r = await (await fetch(U('/api/world/mca'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'delete', dimension: MCA_ACTIVE_DIM, files })
      })).json();
      showToast(r.msg || 'Regiones eliminadas', r.ok ? 'success' : 'error');
      if (r.ok) {
        addNotif(`Borrado MCA: ${files.length} regiones en ${MCA_ACTIVE_DIM}`, 'Ahora', 'trash');
        MCA_SEL[MCA_ACTIVE_DIM] = new Set();
        mapInvalidateFiles(files);
      }
    }
    await loadWorldManager();
    await mapRefreshDetail();
    updateMcaSelectionCount();
  } catch (e) {
    showToast('Error de conexión', 'error');
  }
}
async function mcaResetDim(dim) {
  if (!confirm(`⚠️ ALERTA: ¿Deseas reiniciar por completo la dimensión ${dim.toUpperCase()}?

Toda construcción, portal o progreso en esa dimensión desaparecerá.`)) return;
  try {
    const r = await (await fetch(U('/api/world/mca'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'reset_dimension', dimension: dim })
    })).json();
    showToast(r.msg || 'Dimensión reiniciada', r.ok ? 'success' : 'error');
    if (r.ok) {
      addNotif(`Dimensión reiniciada: ${dim.toUpperCase()}`, 'Ahora', 'rotate-ccw');
      mapInvalidateDim(dim);
      await loadWorldManager();
      await mapRefreshDetail();
      updateMcaSelectionCount();
    }
  } catch (e) {
    showToast('Error al reiniciar dimensión', 'error');
  }
}

/* ══════════════════════════════════════════════════
   FLOTA MULTI-SERVIDOR (Fleet View)
══════════════════════════════════════════════════ */
async function loadFleetView() {
  const box = document.getElementById('fleetCardsContainer');
  if (!box) return;
  try {
    const d = await (await fetch(U('/api/fleet'))).json();
    const srvs = (d && d.servers) || [];
    if (!srvs.length) {
      box.innerHTML = '<div class="scard" style="padding:16px">Sin servidores registrados.</div>';
      return;
    }
    box.innerHTML = srvs.map(s => `
      <div class="scard" style="padding:18px; display:flex; flex-direction:column; justify-content:space-between; ${s.current ? 'border-color:#a855f7; box-shadow:0 0 20px rgba(168,85,247,0.3)' : ''}">
        <div>
          <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px">
            <div>
              <div style="font-size:18px; font-weight:800; color:#fff">${escHtml(s.name)}${s.current ? ' <span class="mchip ld">ACTUAL</span>' : ''}</div>
              <div style="font-size:11.5px; color:var(--text-dim)">${escHtml(s.server_dir)}</div>
            </div>
            <span class="status-pill ${s.running ? 'RUNNING' : 'OFFLINE'}">
              <span class="status-pulse"></span>
              <span>${s.running ? 'ONLINE' : 'OFFLINE'}</span>
            </span>
          </div>
          <div style="display:flex; gap:16px; margin:14px 0; font-size:12.5px">
            <div><span style="color:var(--text-dim)">Puerto:</span> <b>:${s.mc_port}</b></div>
            <div><span style="color:var(--text-dim)">RAM Máx:</span> <b>${s.max_mem_gb} GB</b></div>
          </div>
        </div>
        <div style="display:flex; gap:8px; margin-top:10px">
          ${s.current ? '<button class="term-tool-btn" style="flex:1" disabled>Servidor en uso</button>' : `<button class="cmd-btn" style="flex:1" onclick="location.search='?server='+encodeURIComponent('${s.id}')">Conmutar a este</button>`}
          <button class="term-tool-btn" onclick="serverAction('${s.running ? 'restart' : 'start'}')">${s.running ? 'Reiniciar' : 'Iniciar'}</button>
        </div>
      </div>`).join('');
  } catch (e) {
    box.innerHTML = '<div class="scard" style="padding:16px">Error al cargar flota de servidores.</div>';
  }
}

/* ══════════════════════════════════════════════════
   GLOBAL COMMAND PALETTE (Ctrl + K)
══════════════════════════════════════════════════ */
const PALETTE_ACTIONS = [
  { title: 'Consola del servidor', cat: 'Navegar', action: () => switchTab('console') },
  { title: 'Métricas del sistema & TPS', cat: 'Navegar', action: () => switchTab('metrics') },
  { title: 'Archivos y Mods', cat: 'Navegar', action: () => switchTab('files') },
  { title: 'Mundos y MCA Region Cleaner', cat: 'Navegar', action: () => switchTab('worlds') },
  { title: 'Copias de seguridad (Backups)', cat: 'Navegar', action: () => switchTab('backups') },
  { title: 'Tareas programadas', cat: 'Navegar', action: () => switchTab('tasks') },
  { title: 'Historial de conexiones', cat: 'Navegar', action: () => switchTab('history') },
  { title: 'Flota de servidores', cat: 'Navegar', action: () => switchTab('fleet') },
  { title: 'Configuración y Ajustes', cat: 'Navegar', action: () => switchTab('settings') },
  { title: 'Iniciar Servidor', cat: 'Control', action: () => serverAction('start') },
  { title: 'Reiniciar Servidor', cat: 'Control', action: () => serverAction('restart') },
  { title: 'Detener Servidor', cat: 'Control', action: () => serverAction('stop') },
  { title: 'Reload Servidor (Datapacks/Mods)', cat: 'Control', action: () => serverAction('reload') },
  { title: 'Hacer Backup ahora', cat: 'Backups', action: () => { switchTab('backups'); setTimeout(backupNow, 300); } },
  { title: 'Diagnóstico de Salud Spark', cat: 'Rendimiento', action: () => sparkQuickAction('health') },
  { title: 'Explorador de Mods Modrinth', cat: 'Mods', action: () => { switchTab('files'); setTimeout(toggleModrinthView, 300); } },
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

const TAB_HASH = { consola: 'console', metricas: 'metrics', sistema: 'metrics', archivos: 'files', mundos: 'worlds', mca: 'worlds', backups: 'backups', tareas: 'tasks', historial: 'history', jugadores: 'history', flota: 'fleet', servidores: 'fleet', diagnostico: 'diag', configuracion: 'settings', ajustes: 'settings' };
const TAB_SLUG = { console: 'consola', metrics: 'metricas', files: 'archivos', worlds: 'mundos', backups: 'backups', tasks: 'tareas', history: 'historial', fleet: 'flota', diag: 'diagnostico', settings: 'configuracion' };
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
setInterval(refreshStats, 2000);
setInterval(refreshConsole, 2500);
refreshStats();
refreshConsole();

/* ══════════════════════════════════════════════════
   DIAGNOSTICS TAB — Crash-reports, JVM, Props, Logs
══════════════════════════════════════════════════ */

// ── Crash-reports ──
async function loadCrashes() {
  try {
    const d = await (await fetch(U('/api/crashes'))).json();
    if (!d.ok) return;
    const g = d.guard || {};
    const badge = document.getElementById('crashGuardBadge');
    if (g.blocked) {
      badge.innerHTML = '<span style="color:#f87171;font-weight:700">\u{1f6d1} Bucle de crash \u2014 inicios autom\u00e1ticos bloqueados</span> <button class="term-tool-btn" onclick="crashUnblock()" style="margin-left:6px">Desbloquear</button>';
    } else if (g.enabled) {
      badge.innerHTML = '<span style="color:#4ade80;font-size:12px">\u2713 Guarda activa (' + g.recent + '/' + g.max + ' en ' + g.window_min + ' min)</span>';
    } else {
      badge.innerHTML = '<span style="color:var(--text-dim);font-size:12px">Guarda desactivada</span>';
    }
    const gp = document.getElementById('crashGuardPanel');
    gp.innerHTML = '<details style="font-size:12px;margin-bottom:4px"><summary style="cursor:pointer;color:var(--text-muted)">Configurar guarda de bucle</summary>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px;align-items:center">' +
      '<label><input type="checkbox" id="cgEnabled"' + (g.enabled ? ' checked' : '') + '> Activada</label> ' +
      '<label><input type="checkbox" id="cgRestart"' + (g.restart ? ' checked' : '') + '> Reiniciar tras una ca\u00edda (20 s)</label>' +
      ' M\u00e1x ca\u00eddas: <input type="number" id="cgMax" value="' + (g.max||3) + '" min="1" max="20" style="width:50px" class="tin">' +
      ' en <input type="number" id="cgWin" value="' + (g.window_min||10) + '" min="1" max="240" style="width:60px" class="tin"> min' +
      ' <button class="term-tool-btn" onclick="crashGuardSave()">Guardar</button></div></details>';

    const list = document.getElementById('crashList');
    if (!d.items.length) { list.innerHTML = '<div class="file-row"><span style="color:var(--text-dim)">Sin crash-reports</span></div>'; return; }
    const kindLabel = {arranque: '\u{1f527} Arranque', watchdog: '\u23f1 Watchdog', 'ejecuci\u00f3n': '\u{1f4a5} Ejecuci\u00f3n', otro: '\u2753'};
    list.innerHTML = d.items.map(c => {
      const dt = new Date(c.time * 1000);
      const ts = dt.toLocaleDateString('es') + ' ' + dt.toLocaleTimeString('es', {hour:'2-digit',minute:'2-digit'});
      return '<div class="file-row" style="cursor:pointer" onclick="openCrash(\'' + escHtml(c.name) + '\')">' +
        '<div style="flex:1;min-width:0"><div style="font-weight:600;font-size:12.5px">' + escHtml(c.name) + '</div>' +
        '<div style="font-size:11.5px;color:var(--text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
        (kindLabel[c.kind]||'') + ' \u00b7 ' + escHtml(c.summary) + '</div></div>' +
        '<div style="display:flex;align-items:center;gap:8px">' +
        (c.suspect ? '<span class="file-badge" style="background:rgba(248,113,113,.15);color:#f87171">' + escHtml(c.suspect) + '</span>' : '') +
        '<span style="font-size:11px;color:var(--text-dim)">' + ts + '</span>' +
        '<button class="icon-btn" title="Eliminar" onclick="event.stopPropagation();crashDel(\'' + escHtml(c.name) + '\')">\u{1f5d1}</button>' +
        '</div></div>';
    }).join('');
  } catch (e) {}
}
async function openCrash(name) {
  try {
    const d = await (await fetch(U('/api/crashes?name=' + encodeURIComponent(name)))).json();
    if (!d.ok) return;
    const c = d.crash;
    const det = document.getElementById('crashDetail');
    let h = '<h3 style="margin-bottom:8px">' + escHtml(c.name) + '</h3>';
    h += '<div style="display:flex;gap:12px;flex-wrap:wrap;font-size:12.5px;margin-bottom:10px">';
    h += '<span><b>Tipo:</b> ' + escHtml(c.kind) + '</span>';
    if (c.suspect) h += '<span><b>Sospechoso:</b> <span style="color:#f87171">' + escHtml(c.suspect) + '</span></span>';
    h += '</div>';
    if (c.mods && c.mods.length) {
      h += '<div class="system-details-card" style="margin-bottom:8px;padding:10px"><h4 style="margin-bottom:6px">Mods implicados</h4>';
      c.mods.forEach(m => {
        h += '<div style="margin-bottom:4px"><b>' + escHtml(m.id || '?') + '</b>';
        if (m.version) h += ' <span style="color:var(--text-dim)">' + escHtml(m.version) + '</span>';
        if (m.file) h += ' <span style="color:var(--text-dim)">(' + escHtml(m.file) + ')</span>';
        if (m.detail) h += '<div style="font-size:12px;color:#facc15">' + escHtml(m.detail) + '</div>';
        h += '</div>';
      });
      h += '</div>';
    }
    if (c.hints && c.hints.length) {
      h += '<div style="margin-bottom:8px">';
      c.hints.forEach(t => { h += '<div style="font-size:12.5px;padding:6px 10px;border-radius:8px;background:rgba(74,222,128,.08);color:#4ade80;margin-bottom:4px">\u{1f4a1} ' + escHtml(t) + '</div>'; });
      h += '</div>';
    }
    if (c.involved && c.involved.length) {
      h += '<div style="font-size:12px;margin-bottom:8px"><b>Mods en la pila:</b> ';
      h += c.involved.map(i => escHtml(i.id) + ' (' + i.frames + ')').join(', ') + '</div>';
    }
    if (c.exception) h += '<div style="font-size:12px;margin-bottom:8px;word-break:break-all"><b>Excepci\u00f3n:</b> ' + escHtml(c.exception) + '</div>';
    h += '<details style="margin-top:8px"><summary style="cursor:pointer;font-size:12px">Texto completo del crash-report</summary>';
    h += '<pre style="font-size:11px;max-height:400px;overflow:auto;background:var(--bg-terminal);padding:10px;border-radius:8px;white-space:pre-wrap;word-break:break-all">' + escHtml(c.text || '') + '</pre></details>';
    det.innerHTML = h;
    document.getElementById('crashModal').style.display = 'flex';
  } catch (e) {}
}
async function crashDel(name) {
  if (!confirm('\u00bfEliminar ' + name + '?')) return;
  try {
    await fetch(U('/api/crashes'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'delete',name})});
    loadCrashes();
  } catch (e) {}
}
async function crashUnblock() {
  try {
    const r = await (await fetch(U('/api/crashes'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'unblock'})})).json();
    showToast(r.msg); loadCrashes();
  } catch (e) {}
}
async function crashGuardSave() {
  try {
    const r = await (await fetch(U('/api/crashes'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
      action:'config', enabled:document.getElementById('cgEnabled').checked, restart:document.getElementById('cgRestart').checked,
      max:parseInt(document.getElementById('cgMax').value)||3,
      window_min:parseInt(document.getElementById('cgWin').value)||10
    })})).json();
    showToast(r.msg); loadCrashes();
  } catch (e) {}
}

// \u2500\u2500 JVM args \u2500\u2500
let _jvmPresets = {};
async function jvmLoad() {
  try {
    const d = await (await fetch(U('/api/jvm'))).json();
    if (!d.ok) return;
    document.getElementById('jvmText').value = d.text || '';
    _jvmPresets = d.presets || {};
    const info = d.info || {};
    document.getElementById('jvmInfo').innerHTML =
      'Xms: ' + (info.xms_mb ? info.xms_mb + ' MB' : '-') +
      ' \u00b7 Xmx: ' + (info.xmx_mb ? info.xmx_mb + ' MB' : '-') +
      ' \u00b7 Flags: ' + (info.flags||0) +
      ' \u00b7 Carga serializada: ' + (info.serial_load ? '\u2713' : '\u2717') +
      ' \u00b7 RAM sistema: ' + (d.system_ram_mb ? Math.round(d.system_ram_mb/1024*10)/10 + ' GB' : '-');
    const box = document.getElementById('jvmPresets');
    box.innerHTML = Object.entries(_jvmPresets).map(([k,v]) =>
      '<button class="term-tool-btn" onclick="jvmApplyPreset(\'' + k + '\')" title="Aplica ' + escHtml(v.name) + '">' + escHtml(v.name) + '</button>'
    ).join('') +
      '<button class="term-tool-btn" onclick="jvmToggleSerial()" title="A\u00f1ade/quita -Djava.util.concurrent.ForkJoinPool.common.parallelism=1">\u00b1 Carga serializada</button>';
    document.getElementById('jvmValidation').innerHTML = '';
  } catch (e) {}
}
function jvmApplyPreset(key) {
  const p = _jvmPresets[key];
  if (!p) return;
  const ta = document.getElementById('jvmText');
  const lines = ta.value.split(String.fromCharCode(10)).filter(l => l.trim() && !l.trim().startsWith('#'));
  const mem = lines.filter(l => /^-Xm[sx]/i.test(l.trim()));
  const serial = lines.filter(l => l.includes('ForkJoinPool.common.parallelism'));
  const rest = [].concat(mem.length ? mem : ['-Xms4G', '-Xmx7G'], p.flags, serial);
  ta.value = '# Preset: ' + p.name + String.fromCharCode(10) + rest.join(String.fromCharCode(10)) + String.fromCharCode(10);
}
function jvmToggleSerial() {
  const ta = document.getElementById('jvmText');
  const flag = '-Djava.util.concurrent.ForkJoinPool.common.parallelism=1';
  if (ta.value.includes(flag)) {
    ta.value = ta.value.split(String.fromCharCode(10)).filter(l => !l.includes('ForkJoinPool.common.parallelism')).join(String.fromCharCode(10));
    showToast('Carga serializada eliminada');
  } else {
    ta.value = ta.value.trimEnd() + String.fromCharCode(10) + flag + String.fromCharCode(10);
    showToast('Carga serializada a\u00f1adida');
  }
}
async function jvmValidate() {
  try {
    const r = await (await fetch(U('/api/jvm/validate'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:document.getElementById('jvmText').value})})).json();
    let h = '';
    if (r.errors && r.errors.length) h += r.errors.map(e => '<div style="color:#f87171">\u274c ' + escHtml(e) + '</div>').join('');
    if (r.warnings && r.warnings.length) h += r.warnings.map(w => '<div style="color:#facc15">\u26a0 ' + escHtml(w) + '</div>').join('');
    if (r.ok && !h) h = '<div style="color:#4ade80">\u2713 V\u00e1lido</div>';
    document.getElementById('jvmValidation').innerHTML = h;
  } catch (e) {}
}
async function jvmSave() {
  try {
    const r = await (await fetch(U('/api/jvm'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:document.getElementById('jvmText').value})})).json();
    showToast(r.msg || (r.ok ? 'Guardado' : 'Error'), r.ok ? 'success' : 'error');
    let h = '';
    if (r.warnings && r.warnings.length) h = r.warnings.map(w => '<div style="color:#facc15">\u26a0 ' + escHtml(w) + '</div>').join('');
    document.getElementById('jvmValidation').innerHTML = h;
    if (r.ok) jvmLoad();
  } catch (e) { showToast('Error al guardar'); }
}

// \u2500\u2500 server.properties (formulario) \u2500\u2500
let _propsData = null;
async function propsLoad() {
  try {
    const d = await (await fetch(U('/api/props'))).json();
    if (!d.ok) return;
    _propsData = d;
    const box = document.getElementById('propsForm');
    let h = '';
    (d.groups || []).forEach(g => {
      h += '<details open style="margin-bottom:10px"><summary style="cursor:pointer;font-weight:700;font-size:13px;margin-bottom:6px">' + escHtml(g.name) + '</summary>';
      h += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px">';
      g.items.forEach(it => {
        const id = 'prop_' + it.key.replace(/[.]/g, '_');
        h += '<div style="padding:6px 0">';
        h += '<label style="font-size:12px;font-weight:600;display:block;margin-bottom:2px" for="' + id + '">' + escHtml(it.key);
        if (it.restart) h += ' <span style="color:#facc15;font-size:10px" title="Requiere reinicio">\u27f3</span>';
        h += '</label>';
        if (it.desc) h += '<div style="font-size:11px;color:var(--text-dim);margin-bottom:2px">' + escHtml(it.desc) + '</div>';
        if (it.type === 'bool') {
          h += '<select id="' + id + '" class="tin" data-prop="' + escHtml(it.key) + '" style="width:100%"><option value="true"' + (it.value==='true'?' selected':'') + '>true</option><option value="false"' + (it.value==='false'?' selected':'') + '>false</option></select>';
        } else if (it.type === 'enum') {
          h += '<select id="' + id + '" class="tin" data-prop="' + escHtml(it.key) + '" style="width:100%">';
          (it.options||[]).forEach(o => { h += '<option value="' + o + '"' + (it.value===o?' selected':'') + '>' + o + '</option>'; });
          h += '</select>';
        } else if (it.type === 'int') {
          h += '<input type="number" id="' + id + '" class="tin" data-prop="' + escHtml(it.key) + '" value="' + escHtml(String(it.value)) + '" min="' + it.min + '" max="' + it.max + '" style="width:100%">';
        } else {
          h += '<input type="' + (it.secret ? 'password' : 'text') + '" id="' + id + '" class="tin" data-prop="' + escHtml(it.key) + '" value="' + escHtml(String(it.value)) + '" style="width:100%"' + (it.has_secret ? ' placeholder="(oculto \u2014 deja vac\u00edo para no cambiar)"' : '') + '>';
        }
        h += '</div>';
      });
      h += '</div></details>';
    });
    box.innerHTML = h;
    document.getElementById('propsMsg').innerHTML = '';
  } catch (e) {}
}
async function propsSave() {
  const changes = {};
  document.querySelectorAll('[data-prop]').forEach(el => {
    const k = el.dataset.prop;
    const v = el.value;
    if (el.type === 'password' && !v) return;
    changes[k] = v;
  });
  try {
    const r = await (await fetch(U('/api/props'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({changes})})).json();
    let msg = escHtml(r.msg || '');
    if (r.restart && r.restart.length) msg += ' <span style="color:#facc15">\u27f3 Requiere reinicio: ' + r.restart.map(escHtml).join(', ') + '</span>';
    document.getElementById('propsMsg').innerHTML = msg;
    showToast(r.msg, r.ok ? 'success' : 'error');
    if (r.ok) propsLoad();
  } catch (e) { showToast('Error'); }
}

// \u2500\u2500 Visor de logs \u2500\u2500
let _logFilesLoaded = false;
async function logsInit() {
  if (!_logFilesLoaded) {
    try {
      const d = await (await fetch(U('/api/logs?list=1'))).json();
      if (d.ok && d.files) {
        const sel = document.getElementById('dgLogFile');
        sel.innerHTML = d.files.map(f => '<option value="' + escHtml(f.name) + '">' + escHtml(f.name) + ' (' + (f.size > 1048576 ? (f.size/1048576).toFixed(1)+'MB' : Math.round(f.size/1024)+'KB') + ')</option>').join('');
        _logFilesLoaded = true;
      }
    } catch (e) {}
  }
  logsLoad();
}
async function logsLoad() {
  const name = document.getElementById('dgLogFile').value || 'latest.log';
  const level = document.getElementById('dgLogLevel').value || 'ALL';
  const q = document.getElementById('dgLogSearch').value || '';
  try {
    const url = U('/api/logs?name=' + encodeURIComponent(name) + '&level=' + level + '&q=' + encodeURIComponent(q) + '&limit=600');
    const d = await (await fetch(url)).json();
    if (!d.ok) { showToast(d.msg || 'Error'); return; }
    const counts = d.counts || {};
    document.getElementById('dgLogCounts').innerHTML =
      Object.entries(counts).map(([k,v]) => {
        const colors = {INFO:'#94a3b8',WARN:'#facc15',ERROR:'#f87171',FATAL:'#ef4444',DEBUG:'#64748b',TRACE:'#475569'};
        return '<span style="color:' + (colors[k]||'#ccc') + '">' + k + ': ' + v + '</span>';
      }).join(' \u00b7 ') + (d.truncated ? ' <span style="color:#facc15">(truncado a ' + d.shown + ' de ' + d.total + ')</span>' : '');
    const box = document.getElementById('dgLogEntries');
    if (!d.entries.length) { box.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-dim)">Sin entradas para este filtro</div>'; return; }
    const levelCls = {INFO:'log-info',WARN:'log-warn',ERROR:'log-err',FATAL:'log-err',DEBUG:'log-info'};
    box.innerHTML = d.entries.map(e => '<div class="' + (levelCls[e.level]||'') + '" style="padding:1px 0;white-space:pre-wrap;word-break:break-all">' + escHtml(e.text) + '</div>').join('');
    box.scrollTop = box.scrollHeight;
  } catch (e) {}
}
function logsDownload() {
  const name = document.getElementById('dgLogFile').value || 'latest.log';
  window.open(U('/api/logs/download?name=' + encodeURIComponent(name)));
}

(function initTab() {
  try {
    let t = tabFromHash();
    if (!t) {
      try {
        const s = localStorage.getItem('pkTab');
        if (s && KNOWN_TABS.includes(s)) {
          t = s;
          if (s === 'files') {
            const fp = localStorage.getItem('pkFs') || '';
            if (fp) fsPath = fp;
          }
        }
      } catch (e) {}
    }
    if (t) switchTab(t, true);
  } catch (e) {}
})();
window.addEventListener('load', () => {
  try {
    const t = tabFromHash();
    const cur = document.querySelector('.tab-content.active');
    if (t && (!cur || cur.id !== 'tab-' + t)) switchTab(t, true);
  } catch (e) {}
});
