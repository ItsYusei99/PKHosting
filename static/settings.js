// ── TOTP ──
let _totpUri = '';
async function totpStatus() {
  try {
    const d = await (await fetch(U('/api/totp'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'status'})})).json();
    document.getElementById('totpState').textContent = d.enabled ? 'Activado: el login pedirá contraseña + código.' : 'Desactivado.';
  } catch (e) {}
}
async function totpSetup() {
  try {
    const d = await (await fetch(U('/api/totp'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'setup'})})).json();
    if (!d.ok) { showToast(d.msg || 'Error'); return; }
    _totpUri = d.uri;
    document.getElementById('totpSecret').textContent = d.secret;
    document.getElementById('totpSetup').style.display = 'block';
  } catch (e) { showToast('Error'); }
}
function totpCopyUri() {
  try { navigator.clipboard.writeText(_totpUri); showToast('Enlace copiado'); } catch (e) { showToast(_totpUri); }
}
async function totpEnable() {
  try {
    const d = await (await fetch(U('/api/totp'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'enable', code:document.getElementById('totpCode').value})})).json();
    showToast(d.msg || 'OK', d.ok ? 'success' : 'error');
    if (d.ok) { document.getElementById('totpSetup').style.display = 'none'; totpStatus(); }
  } catch (e) { showToast('Error'); }
}
async function totpDisable() {
  try {
    const v = document.getElementById('totpOff').value;
    const body = /^\d{6}$/.test(v.trim()) ? {action:'disable', code:v.trim()} : {action:'disable', code:'', current:v};
    const d = await (await fetch(U('/api/totp'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})).json();
    showToast(d.msg || 'OK', d.ok ? 'success' : 'error');
    if (d.ok) totpStatus();
  } catch (e) { showToast('Error'); }
}

/* ═══ TEMAS + A11Y + PLANTILLAS ═══ */
const ACCENTS = {
  morado: { ac: '#a855f7', ac2: '#7c3aed', rgb: '168,85,247', soft: '#c084fc', label: 'Morado' },
  cian: { ac: '#0891b2', ac2: '#0e7490', rgb: '8,145,178', soft: '#67e8f9', label: 'Cian' },
  verde: { ac: '#16a34a', ac2: '#15803d', rgb: '22,163,74', soft: '#86efac', label: 'Verde' },
  ambar: { ac: '#d97706', ac2: '#b45309', rgb: '217,119,6', soft: '#fcd34d', label: '\u00c1mbar' },
  rosa: { ac: '#db2777', ac2: '#be185d', rgb: '219,39,119', soft: '#f9a8d4', label: 'Rosa' }
};
function applyTheme() {
  // Sin preferencia guardada se sigue la del sistema
  let th = matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark', ac = 'morado';
  try { th = localStorage.getItem('pkTheme') || th; ac = localStorage.getItem('pkAccent') || 'morado'; } catch (e) {}
  document.body.dataset.theme = th;
  const a = ACCENTS[ac] || ACCENTS.morado;
  const r = document.documentElement.style;
  r.setProperty('--ac', a.ac); r.setProperty('--ac2', a.ac2);
  r.setProperty('--ac-rgb', a.rgb); r.setProperty('--acsoft', a.soft);
  renderAccentRow(ac);
  paintThemeBtn(th);
  document.dispatchEvent(new Event('pk-theme'));
}
function paintThemeBtn(th) {
  const use = document.querySelector('#themeBtn use');
  if (use) use.setAttribute('href', '/static/icons.svg?v=__V__#i-' + (th === 'light' ? 'moon' : 'sun'));
}
function toggleTheme() {
  const next = (document.body.dataset.theme === 'light') ? 'dark' : 'light';
  try { localStorage.setItem('pkTheme', next); } catch (e) {}
  document.body.dataset.theme = next;
  paintThemeBtn(next);
  document.dispatchEvent(new Event('pk-theme'));
}
function setAccent(name) {
  try { localStorage.setItem('pkAccent', name); } catch (e) {}
  applyTheme();
}
function renderAccentRow(cur) {
  const box = document.getElementById('accentRow');
  if (!box) return;
  box.innerHTML = Object.entries(ACCENTS).map(([k, a]) =>
    '<button class="accent-sw' + (k === cur ? ' on' : '') + '" role="radio" aria-checked="' + (k === cur) + '"' +
    ' aria-label="Acento ' + a.label + '" title="' + a.label + '" style="background:' + a.ac + ';color:' + a.ac + '" onclick="setAccent(\'' + k + '\')"></button>'
  ).join('');
}
const CMD_TEMPLATES = [
  'say ¡Hola a todos!', 'list', 'save-all', 'time set day', 'weather clear',
  'difficulty normal', 'gamemode survival @a', 'whitelist on', 'whitelist list'
];
function cmdTplInit() {
  const sel = document.getElementById('cmdTpl');
  if (!sel || sel.options.length > 1) return;
  CMD_TEMPLATES.forEach(c => {
    const o = document.createElement('option');
    o.value = c; o.textContent = c;
    sel.appendChild(o);
  });
}
function cmdTplPick() {
  const sel = document.getElementById('cmdTpl');
  if (sel && sel.value) {
    document.getElementById('cmdInput').value = sel.value;
    document.getElementById('cmdInput').focus();
    sel.value = '';
  }
}
function navKeys(e) {
  const items = Array.from(document.querySelectorAll('.nav-item'));
  const i = items.indexOf(document.activeElement);
  if (i < 0) return;
  let n = null;
  if (e.key === 'ArrowDown' || e.key === 'ArrowRight') n = (i + 1) % items.length;
  else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') n = (i - 1 + items.length) % items.length;
  else if (e.key === 'Home') n = 0;
  else if (e.key === 'End') n = items.length - 1;
  if (n !== null) { e.preventDefault(); items[n].focus(); items[n].click(); }
}
function navA11y() {
  document.querySelectorAll('.nav-item').forEach(el => {
    el.setAttribute('role', 'tab');
    el.setAttribute('tabindex', el.classList.contains('active') ? '0' : '-1');
    if (!el.hasAttribute('data-navkeys')) { el.setAttribute('data-navkeys', '1'); el.addEventListener('keydown', navKeys); }
  });
  const nav = document.querySelector('nav[role="tablist"]');
  if (nav) {
    document.querySelectorAll('.tab-content').forEach(t => t.setAttribute('role', 'tabpanel'));
  }
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const m = document.getElementById('crashModal');
    if (m && m.style.display !== 'none') m.style.display = 'none';
    if (document.querySelector('dialog.pk-dialog[open]')) return;   // lo gestiona el propio <dialog>
    const fe = document.getElementById('fsEditModal');
    if (fe && fe.style.display === 'flex') fsCloseEdit();
    const sb = document.getElementById('srvBrowseModal');
    if (sb && sb.style.display === 'flex') srvBrowseClose();
    const pm = document.getElementById('playerModal');
    if (pm && pm.style.display === 'flex') closePlayerModal();
    const nd = document.getElementById('notifDropdown');
    if (nd && nd.style.display === 'block') { nd.style.display = 'none'; }
  }
});
