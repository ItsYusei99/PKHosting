async function refreshStats() {
  try {
    const res = await fetch(U('/api/stats'));
    const d = await res.json();
    
    // Status Badge
    const badge = document.getElementById('statusBadge');
    const stText = document.getElementById('statusText');
    const sideDot = document.getElementById('sideDot');
    badge.className = 'status-pill ' + d.status;
    stText.textContent = STATUS_LABEL[d.status] || d.status;
    const hs = document.getElementById('heroStatus');
    if (hs) { hs.textContent = STATUS_LABEL[d.status] || d.status; hs.dataset.status = d.status; }

    const colorMap = {
      'RUNNING': 'var(--ok)',
      'STARTING': 'var(--warn)',
      'STOPPING': 'var(--warn)',
      'OFFLINE': 'var(--bad)'
    };
    sideDot.style.background = colorMap[d.status] || 'var(--bad)';

    // Buttons Disable
    document.getElementById('btnStart').disabled = (d.status !== 'OFFLINE');
    document.getElementById('btnStop').disabled = (d.status === 'OFFLINE');
    document.getElementById('btnRestart').disabled = (d.status === 'OFFLINE');
    document.getElementById('btnReload').disabled = (d.status !== 'RUNNING');
    document.getElementById('btnKill').disabled = (d.status === 'OFFLINE');
    if (d.public_ip) setPublicIp(d.public_ip);

    // Cards
    document.getElementById('cardUptime').textContent = d.status === 'OFFLINE' ? '—' : d.uptime;
    document.getElementById('cardUptimeSub').textContent = d.status === 'RUNNING' ? 'En línea y respondiendo' : (d.status === 'STARTING' ? 'Cargando mods y mundo…' : 'Servidor detenido');
    
    // Color por nivel de uso: verde < umbral1, ámbar < umbral2, rojo arriba
    const cpuPct = Math.max(0, d.cpu || 0);
    if (d.max_mem_gb) MEM_MAX_GB = d.max_mem_gb;
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
    document.getElementById('cardMemSub').textContent = `de ${d.max_mem_gb} GB asignados`;
    setText('homeCpuNow', d.cpu.toFixed(1) + '%');
    setText('homeMemNow', d.mem_gb + ' GB');

    document.getElementById('cardPlayers').innerHTML = `${d.online_players}<small>/ ${d.max_players}</small>`;
    const bp = document.getElementById('barPlayers');
    if (bp) bp.style.width = (d.max_players ? Math.min(100, d.online_players / d.max_players * 100) : 0) + '%';
    const mgmt = document.getElementById('playerMgmt');
    const rows = document.getElementById('playerRows');
    const hasPlayers = d.online_players > 0 && d.player_names && d.player_names.length;
    const homeBox = document.getElementById('homePlayers');
    const homeCnt = document.getElementById('homePlayersCount');
    if (homeCnt) homeCnt.textContent = hasPlayers ? String(d.player_names.length) : '';
    if (homeBox) {
      // Inventario: una ranura por jugador y ranuras vacías hasta completar la fila de 9
      const names = hasPlayers ? d.player_names : [];
      const sess = d.sessions || {};
      const sig = names.join('|') + '#' + names.map(n => Math.floor((sess[n] || 0) / 60)).join(',');
      if (sig !== homeBox.dataset.sig) {
        homeBox.dataset.sig = sig;
        const slots = names.map(n => `<button type="button" class="inv-slot" onclick="openPlayerModal(${jsArg(n)})" title="Ver ficha de ${escHtml(n)}">`
          + `<img src="https://mc-heads.net/avatar/${encodeURIComponent(n)}/36" alt="" onerror="this.src='/icon.svg'">`
          + `<span class="inv-name">${escHtml(n)}</span>`
          + (sess[n] != null ? `<span class="inv-time">${fmtSess(sess[n])}</span>` : '') + '</button>');
        const pad = Math.max(0, (Math.ceil(Math.max(names.length, 1) / 9) * 9) - names.length);
        for (let i = 0; i < pad; i++) slots.push('<div class="inv-slot empty" aria-hidden="true"></div>');
        homeBox.innerHTML = (names.length ? '' : '<div class="empty-state inv-empty">Nadie conectado ahora mismo</div>') + slots.join('');
      }
    }
    if (rows && !hasPlayers) { window._plSig = ''; rows.innerHTML = '<div class="empty-state">Nadie conectado</div>'; }
    const plSig = hasPlayers ? d.player_names.join('|') + '#' + d.player_names.map(n => Math.floor(((d.sessions || {})[n] || 0) / 60)).join(',') : '';
    if (rows && hasPlayers && plSig !== window._plSig) {
      window._plSig = plSig;
      const sess = d.sessions || {};
      rows.innerHTML = d.player_names.map(n => {
        const safe = escHtml(n);
        const t = sess[n] != null ? ` · <span style="font-size:11px;color:var(--text-dim)">${fmtSess(sess[n])}</span>` : '';
        return `<div class="file-row">
          <img class="p-avatar" src="https://mc-heads.net/avatar/${encodeURIComponent(n)}/28" alt="${safe}" onerror="this.src='/icon.svg'">
          <span class="file-name" style="cursor:pointer" onclick="openPlayerModal(${jsArg(n)})" title="Ver ficha del jugador"><b>${safe}</b>${t}</span>
          <span class="file-actions">
            <button class="cmd-btn" title="Ficha del jugador" onclick="openPlayerModal(${jsArg(n)})">Ficha</button>
            <button class="term-tool-btn" title="Dar OP" onclick="playerAction('op', ${jsArg(n)})">OP</button>
            <button class="term-tool-btn" title="Quitar OP" onclick="playerAction('deop', ${jsArg(n)})">DeOP</button>
            <button class="term-tool-btn" title="Expulsar" onclick="playerAction('kick', ${jsArg(n)})">Kick</button>
            <button class="term-tool-btn" style="color:#f87171; border-color:#7f1d1d" title="Banear" onclick="playerAction('ban', ${jsArg(n)})">Ban</button>
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
    const homeTps = document.getElementById('homeTps');
    const homeTpsBar = document.getElementById('homeTpsBar');
    if (homeTps) {
      const ok = t.ok && t.tps != null;
      homeTps.textContent = ok ? t.tps.toFixed(1) : (d.status === 'RUNNING' ? '…' : '—');
      homeTps.dataset.level = ok ? (t.tps >= 19 ? 'ok' : (t.tps >= 15 ? 'warn' : 'bad')) : '';
      homeTpsBar.style.width = ok ? Math.min(100, t.tps / 20 * 100) + '%' : '0%';
      homeTpsBar.style.setProperty('--fill', cssVar('--' + (homeTps.dataset.level || 'ok'), '#4ade80'));
    }
    if (t.ok && t.tps != null) {
      const tc = cssVar(t.tps >= 19 ? '--ok' : (t.tps >= 15 ? '--warn' : '--bad'), '#4ade80');
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

    const crashBanner = document.getElementById('crashBanner');
    if (crashBanner) {
      if (d.crash_blocked) {
        crashBanner.style.display = 'flex';
        document.getElementById('crashBannerMsg').textContent =
          `Bucle de crash: inicios automáticos bloqueados (${d.crash_recent || 0} caídas recientes)`;
      } else {
        crashBanner.style.display = 'none';
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
    if (document.getElementById('tab-home').classList.contains('active')) renderHomeCharts();
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
    ctx.fillStyle = cssVar('--text-3', '#8f86a4');
    ctx.font = '12px ' + cssVar('--font-sans', 'sans-serif');
    ctx.fillText('Esperando datos…', 4, h / 2);
    return;
  }

  // Gridlines
  ctx.strokeStyle = cssVar('--chart-grid', 'rgba(255,255,255,0.06)');
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
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
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
let MEM_MAX_GB = 8;   // se actualiza con max_mem_gb del servidor activo
const STATUS_LABEL = { RUNNING: 'En línea', STARTING: 'Iniciando', STOPPING: 'Deteniendo', OFFLINE: 'Apagado' };
function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
// Los colores salen de los tokens del tema (body[data-theme] los redefine), no de hex fijos
function cssVar(n, fb) { try { return getComputedStyle(document.body).getPropertyValue(n).trim() || fb; } catch (e) { return fb; } }
function usageLevel(pct, warnAt, critAt) { return pct >= critAt ? 'bad' : (pct >= warnAt ? 'warn' : 'ok'); }
function usageColor(pct, warnAt, critAt) {
  return cssVar('--' + usageLevel(pct, warnAt, critAt), '#4ade80');
}
function paintUsage(valEl, barEl, pct, color) {
  const lvl = color === cssVar('--bad') ? 'bad' : (color === cssVar('--warn') ? 'warn' : 'ok');
  valEl.dataset.level = lvl;
  barEl.style.width = pct + '%';
  barEl.style.setProperty('--fill', color);
}
function renderHomeCharts() {
  if (!historyBuffer || historyBuffer.length < 2) return;
  const cpu = historyBuffer.map(h => h.cpu || 0);
  const mem = historyBuffer.map(h => (h.mem || 0) / 1024.0);
  const c1 = cssVar('--ac', '#a855f7'), c2 = cssVar('--info', '#7c9cff');
  drawSmoothChart('canvasHomeCpu', cpu, 100.0, c1, c1 + '33', '%');
  drawSmoothChart('canvasHomeMem', mem, MEM_MAX_GB, c2, c2 + '33', 'GB');
}
// Al cambiar de tema o acento, las gráficas se repintan con los colores nuevos
document.addEventListener('pk-theme', () => { h24At = 0; renderHomeCharts(); if (document.getElementById('tab-metrics').classList.contains('active')) renderCharts(); });
function paintBadge(el, text, color) {
  if (!el) return;
  el.textContent = text;
  el.style.color = color;
  el.style.borderColor = color + '66';
}
let h24At = 0;
let histLimit = 1440;
function histRange(lim) {
  histLimit = lim; h24At = 0;
  document.getElementById('rng24').classList.toggle('active', lim === 1440);
  document.getElementById('rng7d').classList.toggle('active', lim === 10080);
  const lbl = lim === 1440 ? 'últimas 24 h' : 'últimos 7 días';
  document.getElementById('hTitleCpu').textContent = 'CPU — ' + lbl;
  document.getElementById('hTitleMem').textContent = 'RAM — ' + lbl;
  renderCharts();
}
async function renderCharts() {
  if (Date.now() - h24At > 60000) {
    h24At = Date.now();
    try {
      const h = await (await fetch(U('/api/history?limit=' + histLimit))).json();
      if (h.cpu && h.cpu.length > 1) {
        const c1 = cssVar('--ac', '#a855f7'), c2 = cssVar('--ac2', '#7c3aed');
        drawSmoothChart('canvasH24Cpu', h.cpu, 100.0, c1, c1 + '40', '%');
        drawSmoothChart('canvasH24Mem', h.mem, MEM_MAX_GB, c2, c2 + '4D', 'GB');
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
  drawSmoothChart('canvasMem', memVals, MEM_MAX_GB, memStrokeColor, memStrokeColor + '4D', 'GB');
  const cp = document.getElementById('chartCpuPeak');
  if (cp) cp.textContent = chartPeakAvg(cpuVals, 1) + '%';
  const mp = document.getElementById('chartMemPeak');
  if (mp) mp.textContent = chartPeakAvg(memVals, 2) + ' GB';
}
