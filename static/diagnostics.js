// ── Crash-reports ──
async function loadCrashes() {
  try {
    const d = await api('/api/crashes');
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
  if (!await pkConfirm('\u00bfEliminar ' + name + '?')) return;
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
    const d = await api('/api/jvm');
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
    const d = await api('/api/props');
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
      const d = await api('/api/logs?list=1');
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
