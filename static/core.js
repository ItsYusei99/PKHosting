let autoScroll = true;
let historyBuffer = [];
let cmdHistory = [];
let cmdIndex = -1;

const SRV = new URLSearchParams(location.search).get('server') || '';
function U(p) {
  if (!SRV) return p;
  return p + (p.includes('?') ? '&' : '?') + 'server=' + encodeURIComponent(SRV);
}
// Helper común para la API del panel: JSON de ida y vuelta; el wrapper de fetch ya avisa de 5xx/red.
async function api(path, body) {
  const opts = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  return (await fetch(U(path), opts)).json();
}
const _fetch = window.fetch.bind(window);
let _lastNetToast = 0;
function _netErrToast(msg, every) {
  const n = Date.now();
  if (n - _lastNetToast < every) return;
  _lastNetToast = n;
  try { showToast(msg, 'error'); } catch (e) {}
}
window.fetch = async (...a) => {
  let r;
  try { r = await _fetch(...a); }
  catch (e) { _netErrToast('Sin conexión con el panel', 30000); throw e; }
  if (r.status === 401) location.reload();
  else if (r.status >= 500) _netErrToast('Error del servidor (' + r.status + ')', 8000);
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
// Icono del sprite pixel (static/icons.svg); cls opcional: 'ico-sm', 'ico-lg'
function icon(name, cls) {
  return `<svg class="ico${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="/static/icons.svg?v=__V__#i-${name}"/></svg>`;
}
const SILEO_ICONS = {
  info: icon('info-box'),
  success: icon('check'),
  error: icon('close'),
  warning: icon('warning-diamond'),
  loading: icon('reload', 'tk-spin')
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

let currentPublicIp = '__IP__';
function setPublicIp(ip) {
  currentPublicIp = ip;
  ['ipText', 'heroIp'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = ip; });
}
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
    if (d.public_ip) setPublicIp(d.public_ip);
    const ps = document.getElementById('playitStatus');
    if (ps && d.playit) {
      const p = d.playit;
      ps.textContent = p.installed
        ? (p.service_active ? 'Agente playit: servicio activo. ' + (p.note || 'Pega arriba la dirección pública del túnel TCP (puerto __MCPORT__).') : 'Agente playit instalado pero servicio inactivo.')
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

const KNOWN_TABS = ['home', 'console', 'players', 'metrics', 'files', 'mods', 'worlds', 'backups', 'tasks', 'history', 'fleet', 'diag', 'settings'];
// ── Diálogos propios (reemplazan confirm/prompt nativos): foco atrapado, Esc cancela ──
function _pkDialog(build) {
  return new Promise(resolve => {
    const dlg = document.createElement('dialog');
    dlg.className = 'pk-dialog';
    const done = v => { try { dlg.close(); } catch (e) {} dlg.remove(); resolve(v); };
    dlg.addEventListener('cancel', e => { e.preventDefault(); done(null); });
    build(dlg, done);
    document.body.appendChild(dlg);
    dlg.showModal();
    const first = dlg.querySelector('[data-autofocus]');
    if (first) first.focus();
  });
}
function _pkButtons(done, opts, getValue, canOk) {
  const row = document.createElement('div');
  row.className = 'pk-dialog-actions';
  const cancel = document.createElement('button');
  cancel.type = 'button'; cancel.className = 'btn-ghost'; cancel.textContent = opts.cancelText || 'Cancelar';
  cancel.onclick = () => done(null);
  const ok = document.createElement('button');
  ok.type = 'submit'; ok.className = opts.danger ? 'cmd-btn danger' : 'cmd-btn'; ok.textContent = opts.okText || 'Aceptar';
  row.append(cancel, ok);
  return { row, ok, cancel };
}
// confirm: devuelve true/false. opts: { danger, typed (texto a escribir para habilitar), okText }
function pkConfirm(msg, opts = {}) {
  return _pkDialog((dlg, done) => {
    const form = document.createElement('form');
    form.method = 'dialog';
    const p = document.createElement('p');
    p.className = 'pk-dialog-msg'; p.textContent = msg;
    form.appendChild(p);
    let input = null;
    if (opts.typed) {
      const lbl = document.createElement('label');
      lbl.className = 'pk-dialog-lbl'; lbl.textContent = 'Escribe ' + opts.typed + ' para confirmar';
      input = document.createElement('input');
      input.className = 'tin'; input.autocomplete = 'off'; input.setAttribute('data-autofocus', '');
      lbl.appendChild(input); form.appendChild(lbl);
    }
    const { row, ok, cancel } = _pkButtons(done, opts);
    if (input) { ok.disabled = true; input.oninput = () => { ok.disabled = input.value.trim().toUpperCase() !== String(opts.typed).toUpperCase(); }; }
    else (opts.danger ? cancel : ok).setAttribute('data-autofocus', '');
    form.appendChild(row);
    form.onsubmit = e => { e.preventDefault(); if (!ok.disabled) done(true); };
    dlg.appendChild(form);
  }).then(v => v === true);
}
// prompt: devuelve el texto o null si se cancela
function pkPrompt(msg, def = '') {
  return _pkDialog((dlg, done) => {
    const form = document.createElement('form');
    const lbl = document.createElement('label');
    lbl.className = 'pk-dialog-msg'; lbl.textContent = msg;
    const input = document.createElement('input');
    input.className = 'tin'; input.value = def; input.autocomplete = 'off'; input.setAttribute('data-autofocus', '');
    lbl.appendChild(input);
    const { row } = _pkButtons(done, {});
    form.append(lbl, row);
    form.onsubmit = e => { e.preventDefault(); done(input.value); };
    dlg.appendChild(form);
  });
}

function rememberTab(name) {
  try {
    localStorage.setItem('pkTab', name);
    if (name === 'files') localStorage.setItem('pkFs', fsPath);
  } catch (e) {}
}
function switchTab(name, fromHash) {
  if (!fromHash) syncHash(name);
  rememberTab(name);
  try { navA11y(); } catch (e) {}
  document.querySelectorAll('.nav-item').forEach(el => {
    const on = el.dataset.tab === name;
    el.classList.toggle('active', on);
    el.setAttribute('aria-selected', on ? 'true' : 'false');
    el.setAttribute('tabindex', on ? '0' : '-1');
  });
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));

  const tab = document.getElementById('tab-' + name);
  if (tab) tab.classList.add('active');

  if (name === 'metrics') { renderCharts(); loadHeat(); refreshPingMetrics(); }
  if (name === 'files') loadFiles();
  if (name === 'mods') loadMods();
  if (name === 'players') { loadModeration(); }
  if (name === 'worlds') { loadWorldManager(); loadWorldPlayers(); }
  if (name === 'backups') loadBackups();
  if (name === 'tasks') loadSchedules();
  if (name === 'history') loadHistory();
  if (name === 'fleet') loadFleetView();
  if (name === 'diag') { loadCrashes(); jvmLoad(); propsLoad(); logsInit(); }
  if (name === 'console') { loadQuick(); refreshCmdList(); }
  if (name === 'settings') { refreshPublicIp(); totpStatus(); loadDiscord(); loadQuickCfg(); loadServers(); }
}

async function serverAction(act) {
  if (act === 'stop' && !await pkConfirm('¿Detener el servidor? Se guardará el mundo y los jugadores serán expulsados.')) return;
  if (act === 'restart' && !await pkConfirm('¿Reiniciar el servidor ahora? Los jugadores serán expulsados.')) return;
  if (act === 'kill' && !await pkConfirm('¿Matar el proceso del servidor? Puede perder los últimos cambios del mundo.', { danger: true, typed: 'MATAR', okText: 'Matar proceso' })) return;
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

// Acción desde la tarjeta de Flota: opera sobre ESE servidor, no sobre el activo de la página
async function fleetAction(id, act) {
  try {
    const res = await fetch('/api/' + act + '?server=' + encodeURIComponent(id), { method: 'POST' });
    const data = await res.json();
    showToast(data.msg || 'OK');
    setTimeout(loadFleetView, 1500);
  } catch (e) {
    showToast('Error de conexión: ' + e);
  }
}

async function serverRestartPrompt() {
  const v = await pkPrompt('Minutos de aviso antes de reiniciar (0 = inmediato)', '5');
  if (v === null) return;
  const minutes = Math.max(0, Math.min(60, parseInt(v, 10) || 0));
  showToast(minutes ? 'Reinicio con aviso de ' + minutes + ' min...' : 'Reiniciando servidor...');
  try {
    const data = await api('/api/restart', { minutes });
    if (!data.ok && data.cancellable && await pkConfirm(data.msg + '. ¿Cancelarlo?')) {
      const c = await (await fetch(U('/api/restart/cancel'), { method: 'POST' })).json();
      showToast(c.msg || 'OK');
      return;
    }
    showToast(data.msg || 'OK');
    setTimeout(refreshStats, 1200);
  } catch (e) {
    showToast('Error de conexión: ' + e);
  }
}
