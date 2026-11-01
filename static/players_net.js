let heatAt = 0;
async function loadHeat() {
  if (Date.now() - heatAt < 60000) return;
  heatAt = Date.now();
  try {
    const d = await api('/api/activity?days=14');
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
      return `<div class="heat-cell${x.backups ? ' has-bk' : ''}" style="height:${h}px; background:rgba(var(--ac-rgb),${a.toFixed(2)})" title="${x.date}: ${x.joins} joins${x.backups ? ` · ${x.backups} backup(s)` : ''}"></div>`;
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
    if (!await pkConfirm(`¿Vaciar todo el inventario de "${p}"?`)) return;
    cmd = `clear ${p}`;
  }
  if (!cmd) return;
  try {
    const r = await api('/api/cmd', { cmd });
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
    const r = await api('/api/network-ping');
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
    const r = await api('/api/spark', { action: act });
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
          <img src="${escHtml(/^https:\/\//.test(icon) ? icon : '/icon.svg')}" class="mr-icon" alt="" onerror="this.src='/icon.svg'">
          <div style="flex:1; min-width:0">
            <div style="font-weight:700; font-size:13.5px; color:#fff; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">${safeTitle}</div>
            <div style="font-size:11px; color:var(--text-dim)">por ${escHtml(h.author || 'Desconocido')} · ${dlK} descargas</div>
          </div>
        </div>
        <p style="font-size:11.5px; color:#cbd5e1; margin:10px 0 14px; line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden">${safeDesc}</p>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span class="mchip ld">NeoForge</span>
          <button class="cmd-btn" style="padding:4px 12px; font-size:11.5px" onclick="installModrinth(${jsArg(h.slug || h.id)}, ${jsArg(h.title)})">Instalar</button>
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
