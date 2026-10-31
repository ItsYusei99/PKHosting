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
    const noDir = d.writable_msg === 'carpeta inexistente';
    document.getElementById('bkSpaceSub').textContent = noDir ? 'la carpeta aún no existe (se crea al activar los backups)' : `libre en disco: ${d.free_gb} GB`;
    document.getElementById('bkCount').textContent = items.length;
    document.getElementById('bkCountSub').textContent = `${d.monthly || 0} mensuales eternos`;
    const w = d.writable ? '✔ destino escribible' : (noDir ? '⚠ la carpeta de destino no existe todavía' : '✘ destino NO escribible' + (d.writable_msg ? ': ' + d.writable_msg : ''));
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
          <span class="file-name" style="font-size:13.5px">${escHtml(f.name)}${f.monthly ? ' <span class="chart-badge sub">MENSUAL</span>' : ''}<br><span style="font-size:11.5px;color:var(--text-dim)">${escHtml(f.date)} · ${escHtml(f.size)}</span></span>
          <span class="file-actions" style="gap:8px">
          <button class="btn-ghost" onclick="bkDownload(${jsArg(f.name)})">Descargar</button>
          <button class="btn-ghost" onclick="restoreBackup(${jsArg(f.name)})">Restaurar</button>
            <button class="icon-btn danger" title="Eliminar" onclick="deleteBackup(${jsArg(f.name)})">${BK_SVG_TRASH}</button>
          </span>
        </div>`).join('');
    }
    try {
      const c = await api('/api/backup-cfg');
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
  if (!await pkConfirm(`¿RESTAURAR "${name}"?

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
    const r = await api('/api/backup-cfg', body);
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadBackups();
}
async function deleteBackup(name) {
  if (!await pkConfirm(`¿Eliminar el backup "${name}"?`)) return;
  try {
    const r = await api('/api/backup-delete', { name });
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
    const r = await api('/api/password', { current: cur, new: nw });
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
    const d = await api('/api/discord');
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
    const r = await api('/api/discord', { webhook: document.getElementById('dcHook').value.trim(), events: ev });
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
    const d = await api('/api/schedules');
    const box = document.getElementById('skList');
    const items = (d && d.tasks) || [];
    if (!items.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin tareas — el reinicio automático está desactivado hasta que crees una</span></div>'; return; }
    box.innerHTML = items.map(t => {
      const when = t.mode === 'interval' ? `cada ${t.every_min} min` : `diaria ${t.at}`;
      return `<div class="file-row">
        <span class="file-name">${escHtml(t.name)} <span class="chart-badge sub">${escHtml(t.kind)}</span><br><span style="font-size:11px;color:var(--text-dim)">${when} · ${t.enabled ? 'activa' : 'pausada'}</span></span>
        <span class="file-actions">
          <button class="term-tool-btn" title="Ejecutar ahora" onclick="skAct('run',${jsArg(t.id)})">Ahora</button>
          <button class="term-tool-btn" title="Activar/pausar" onclick="skAct('toggle',${jsArg(t.id)})">${t.enabled ? 'Pausar' : 'Activar'}</button>
          <button class="icon-btn danger" title="Eliminar" onclick="skAct('delete',${jsArg(t.id)})">${BK_SVG_TRASH}</button>
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
  if (body.kind === 'restart' && !await pkConfirm('¿Crear tarea de REINICIO automático? (vendrá activada; puedes pausarla cuando quieras)')) return;
  try {
    const r = await api('/api/schedule', { action: 'create', ...body });
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadSchedules();
}
async function skAct(action, id) {
  if (action === 'delete' && !await pkConfirm('¿Eliminar tarea?')) return;
  try {
    const r = await api('/api/schedule', { action, id });
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadSchedules();
}
