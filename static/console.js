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
  if ((act === 'kick' || act === 'ban') && !await pkConfirm(`¿${act === 'ban' ? 'BANEAR' : 'Expulsar'} a "${name}"?`)) return;
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
    const d = await api('/api/lists');
    const chip = (n, act, danger) => `<button class="p-chip"${danger ? ' style="color:var(--bad);border-color:#7f1d1d"' : ''} title="${act}" onclick="modAct(${jsArg(act)},${jsArg(n)})"><img src="https://mc-heads.net/avatar/${encodeURIComponent(n)}/18" class="p-avatar" style="width:16px;height:16px" onerror="this.style.display='none'">${escHtml(n)} ✕</button>`;
    document.getElementById('modOps').innerHTML = (d.ops && d.ops.length) ? d.ops.map(n => chip(n, 'deop')).join(' ') : '—';
    document.getElementById('modBans').innerHTML = (d.banned && d.banned.length) ? d.banned.map(b => chip(b.name, 'pardon', true)).join(' ') : '—';
    _wlOn = !!d.whitelist_on;
    document.getElementById('wlState').textContent = _wlOn ? '[ON]' : '[OFF]';
    document.getElementById('modWl').innerHTML = (d.whitelist && d.whitelist.length) ? d.whitelist.map(n => chip(n, 'wlremove')).join(' ') : '—';
  } catch (e) {}
}
async function loadHistory() {
  try {
    const h = await api('/api/connections');
    const box = document.getElementById('connHist');
    const items = (h && h.items) || [];
    const sub = document.getElementById('histSub');
    if (sub) sub.textContent = items.length ? `${items.length} registros · Entradas y salidas del servidor` : 'Entradas y salidas del servidor';
    if (!items.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin registros</span></div>'; return; }
    box.innerHTML = items.slice(0, 30).map(e => {
      const dt = new Date(e.t * 1000).toLocaleString();
      const what = e.ev === 'join' ? 'entró' : `salió (${fmtSess(e.dur || 0)})`;
      const dot = e.ev === 'join' ? '#34d399' : '#f87171';
      return `<div class="file-row"><img src="https://mc-heads.net/avatar/${encodeURIComponent(e.name)}/22" class="p-avatar" style="width:22px;height:22px" onerror="this.style.display='none'"><span class="file-icon" style="color:${dot}" title="${e.ev === 'join' ? 'Entrada' : 'Salida'}">●</span><span class="file-name" style="cursor:pointer" onclick="openPlayerModal(${jsArg(e.name)})">${escHtml(e.name)} ${what}</span><span class="file-size">${dt}</span></div>`;
    }).join('');
  } catch (e) {}
}
async function modAct(act, name) {
  if ((act === 'wlremove') && !await pkConfirm(`¿Quitar a "${name}" de la whitelist?`)) return;
  try {
    const r = await api('/api/player', { action: act, name });
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadModeration();
}
async function wlAdd() {
  const v = document.getElementById('wlName').value.trim();
  if (!v) return;
  try {
    const r = await api('/api/player', { action: 'wladd', name: v });
    showToast(r.msg || 'OK');
    document.getElementById('wlName').value = '';
  } catch (e) { showToast('Error'); }
  loadModeration();
}
async function wlToggle() {
  try {
    const r = await api('/api/whitelist', { on: !_wlOn });
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
  document.querySelectorAll('#logFilterSeg button').forEach(b => {
    const on = b.dataset.f === logFilter;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
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
let consoleOffset = null, consoleSrc = '';
const CONSOLE_KEEP = 1000;
async function refreshConsole() {
  try {
    const qs = consoleOffset == null ? '' : '?since=' + consoleOffset + '&src=' + encodeURIComponent(consoleSrc);
    const data = await (await fetch(U('/api/console' + qs))).json();
    consoleOffset = data.offset; consoleSrc = data.src || '';
    const add = data.lines || [];
    if (data.reset) {
      if (add.length) { termLines = add; renderConsole(); }
    } else if (add.length) {
      termLines = termLines.concat(add);
      const plain = !logFilter && !document.getElementById('logSearch').value;
      if (plain && termLines.length <= CONSOLE_KEEP) appendConsole(add);   // sin reconstruir: conserva la selección
      else { termLines = termLines.slice(-CONSOLE_KEEP); renderConsole(); }
    }
  } catch (e) {}
}
function appendConsole(lines) {
  const body = document.getElementById('termBody');
  if (!body.innerHTML || body.querySelector('.log-info') && body.textContent.includes('Sin coincidencias')) { renderConsole(); return; }
  const near = body.scrollHeight - body.scrollTop - body.clientHeight < 60;
  body.insertAdjacentHTML('beforeend', '<br>' + lines.map(highlightLogLine).join('<br>'));
  if (autoScroll && (stickBottom || near)) body.scrollTop = body.scrollHeight;
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
    const d = await api('/api/uiconfig');
    const box = document.getElementById('quickCmds');
    box.innerHTML = ((d && d.quick_commands) || []).map(c =>
      `<button class="term-tool-btn" onclick="quickSend(this.dataset.c)" data-c="${c.replace(/"/g, '&quot;')}">${c.replace(/</g, '&lt;')}</button>`
    ).join('');
  } catch (e) {}
}
async function quickSend(c) {
  try {
    const r = await api('/api/cmd', { cmd: c });
    showToast(r.msg || 'OK');
    setTimeout(refreshConsole, 700);
  } catch (e) { showToast('Error'); }
}
async function loadQuickCfg() {
  try {
    const d = await api('/api/uiconfig');
    document.getElementById('quickInput').value = ((d && d.quick_commands) || []).join(', ');
  } catch (e) {}
}
async function saveQuick() {
  const v = document.getElementById('quickInput').value.split(',').map(s => s.trim()).filter(Boolean).slice(0, 12);
  try {
    const r = await api('/api/uiconfig', { quick_commands: v });
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadQuick();
}
