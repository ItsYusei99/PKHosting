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
    const d = await api('/api/world');
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
      <input type="checkbox" class="mca-chk" data-file="${escHtml(r.file)}"${sel.has(r.file) ? ' checked' : ''} onchange="mcaChkToggle(this)">
      <span style="color:#e2e8f0; font-weight:700; width:130px">${escHtml(r.file)}</span>
      <span style="color:var(--text-dim); flex:1">Bloques: [X ${x0}..${x1}, Z ${z0}..${z1}] · ${r.present || 0}/1024 chunks (${pct}%)</span>
      <span style="color:#93c5fd">${mb(r.size)}</span>
      <button class="icon-btn" title="Descargar ${escHtml(r.file)}" onclick="event.stopPropagation();mcaExport(${jsArg(r.file)})">\u2B07</button>
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
const MCA_TILES_MAX = 600;   // tope de tiles en memoria
const MCA_TILE_RETRY_MS = 15000;
function mapQueueTile(key, rx, rz) {
  const prev = MCA_TILES[key];
  // Un tile fallido se reintenta pasado un rato (antes quedaba roto para siempre)
  if (prev && !(prev.failedAt && Date.now() - prev.failedAt > MCA_TILE_RETRY_MS)) return;
  const keys = Object.keys(MCA_TILES);
  if (keys.length >= MCA_TILES_MAX) keys.slice(0, keys.length - MCA_TILES_MAX + 50).forEach(k => delete MCA_TILES[k]);
  const dim = MCA_ACTIVE_DIM;
  const img = new Image();
  const rec = MCA_TILES[key] = { img, ok: false, loading: true };
  img.onload = () => { rec.ok = true; rec.loading = false; drawWorldMap(); try { updateRegionPreview(); } catch (e) {} };
  img.onerror = () => { rec.ok = false; rec.loading = false; rec.failedAt = Date.now(); };
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
  ctx.font = '10px ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace';
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
      ctx.font = '12px ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace';
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
  ctx.font = '13px ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace';
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
function mcaExport(file) {
  window.open(U('/api/world/export?dimension=' + encodeURIComponent(MCA_ACTIVE_DIM) + '&file=' + encodeURIComponent(file)));
}
let PRUNE_CANDS = [];
async function prunePreview() {
  const btn = document.getElementById('prunePrevBtn');
  const st = document.getElementById('pruneStatus');
  const box = document.getElementById('pruneResult');
  btn.disabled = true; PRUNE_CANDS = []; box.innerHTML = '';
  st.textContent = 'Escaneando...';
  try {
    await fetch(U('/api/world/mca'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'prune_preview', dimension: MCA_ACTIVE_DIM,
        max_min: parseFloat(document.getElementById('pruneMin').value) || 0,
        radius: parseInt(document.getElementById('pruneRadius').value) || 0 }) });
    for (let i = 0; i < 120; i++) {
      await new Promise(r => setTimeout(r, 3000));
      const d = await (await fetch(U('/api/world/mca'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'prune_status', dimension: MCA_ACTIVE_DIM }) })).json();
      if (d.error) { st.textContent = 'Error: ' + d.error; btn.disabled = false; return; }
      if (d.done) {
        PRUNE_CANDS = d.candidates || [];
        const files = Object.keys(d.per_file || {}).length;
        st.textContent = d.scanned_files + ' regiones, ' + d.present_chunks + ' chunks (' + (d.truncated ? 'lista truncada a ' + PRUNE_CANDS.length : PRUNE_CANDS.length + ' candidatos') + ')';
        box.innerHTML = '<div>' + PRUNE_CANDS.length + ' chunks con menos de lo indicado en ' + files + ' regiones. Spawn: (' + d.spawn[0] + ', ' + d.spawn[1] + '), radio protegido ' + d.radius_blocks + ' bloques.</div>' +
          (PRUNE_CANDS.length ? '<button class="cmd-btn danger" style="background:#dc2626;color:#fff;margin-top:8px" onclick="pruneApply()">Aplicar poda (' + PRUNE_CANDS.length + ' chunks, servidor detenido, .bak)</button>' : '<div style="color:#4ade80">Nada que podar.</div>');
        btn.disabled = false;
        return;
      }
      st.textContent = 'Escaneando ' + (d.progress || '...') + ' (servidor detenido requerido para aplicar)';
    }
    st.textContent = 'Tiempo agotado esperando el escaneo';
  } catch (e) { st.textContent = 'Error de conexión'; }
  btn.disabled = false;
}
async function pruneApply() {
  if (!PRUNE_CANDS.length) return;
  const NL = String.fromCharCode(10);
  if (!await pkConfirm('¿Podar ' + PRUNE_CANDS.length + ' chunks inactivos de ' + MCA_ACTIVE_DIM.toUpperCase() + '?' + NL + 'Requiere el servidor DETENIDO. Se guarda .bak por región.')) return;
  try {
    const r = await (await fetch(U('/api/world/mca'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'delete_chunks', dimension: MCA_ACTIVE_DIM, chunks: PRUNE_CANDS }) })).json();
    showToast(r.msg || 'OK', r.ok ? 'success' : 'error');
    if (r.ok) { PRUNE_CANDS = []; document.getElementById('pruneResult').innerHTML = ''; mapInvalidateFiles([]); loadWorldManager(); }
  } catch (e) { showToast('Error', 'error'); }
}
async function restoreSelectedChunks() {
  const csel = MCA_CHUNK_SEL[MCA_ACTIVE_DIM] || new Set();
  const chunks = Array.from(csel).map(k => {
    const p = k.split(':');
    return { file: p[0], cx: parseInt(p[1], 10), cz: parseInt(p[2], 10) };
  });
  if (!chunks.length) { showToast('Selecciona chunks sueltos en el mapa (zoom + clic)', 'info'); return; }
  if (!await pkConfirm('¿Restaurar ' + chunks.length + ' chunks desde sus .bak? Requiere el servidor DETENIDO.')) return;
  try {
    const r = await (await fetch(U('/api/world/mca'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'restore_chunks', dimension: MCA_ACTIVE_DIM, chunks }) })).json();
    showToast(r.msg || 'OK', r.ok ? 'success' : 'error');
    if (r.ok) { mapInvalidateFiles(chunks.map(c => c.file)); loadWorldManager(); }
  } catch (e) { showToast('Error', 'error'); }
}
async function loadWorldPlayers() {
  const box = document.getElementById('worldPlayers');
  if (!box) return;
  try {
    const d = await api('/api/world/players');
    if (!d.ok || !d.players.length) { box.innerHTML = '<div class="file-row"><span style="color:var(--text-dim)">Sin datos de jugadores</span></div>'; return; }
    box.innerHTML = d.players.map(p => {
      const pos = (p.x === null || p.x === undefined) ? 'sin posición' : '(' + p.x + ', ' + p.y + ', ' + p.z + ') ' + escHtml(p.dim.replace('minecraft:', ''));
      return '<div class="file-row"><span class="file-name">' + escHtml(p.name) + '</span>' +
        '<span style="color:var(--text-dim); flex:1">' + pos + '</span>' +
        '<span class="mod-state ' + (p.online ? 's-on' : 's-off') + '">' + (p.online ? 'ON' : 'OFF') + '</span>' +
        ((p.x !== null && p.x !== undefined) ? '<button class="term-tool-btn" onclick="centerPlayer(' + p.x + ',' + p.z + ',\'' + escHtml(p.dim) + '\')">Centrar</button>' : '') +
        '</div>';
    }).join('');
  } catch (e) { box.innerHTML = '<div class="file-row">Error al cargar</div>'; }
}
function centerPlayer(x, z, dim) {
  const id = String(dim || '').replace('minecraft:', '');
  if (id && id !== MCA_ACTIVE_DIM && ['overworld', 'nether', 'end'].includes(id)) selectMcaDim(id);
  MCA_VIEW.cx = x / 512; MCA_VIEW.cz = z / 512;
  try { if (typeof CHUNK_ZOOM !== 'undefined' && MCA_VIEW.scale < CHUNK_ZOOM) MCA_VIEW.scale = CHUNK_ZOOM; } catch (e) {}
  drawWorldMap();
  document.getElementById('worldMapCanvas').scrollIntoView({ block: 'nearest' });
  showToast('Mapa centrado en el jugador');
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
  if (!await pkConfirm(warnMsg)) return;
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
  if (!await pkConfirm(`⚠️ ALERTA: ¿Deseas reiniciar por completo la dimensión ${dim.toUpperCase()}?

Toda construcción, portal o progreso en esa dimensión desaparecerá.`, { danger: true, typed: dim.toUpperCase(), okText: 'Reiniciar dimensión' })) return;
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
    const d = await api('/api/fleet');
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
          ${s.current ? '<button class="term-tool-btn" style="flex:1" disabled>Servidor en uso</button>' : `<button class="cmd-btn" style="flex:1" onclick="location.search='?server='+encodeURIComponent(${jsArg(s.id)})">Conmutar a este</button>`}
          <button class="term-tool-btn" onclick="fleetAction(${jsArg(s.id)}, '${s.running ? 'restart' : 'start'}')">${s.running ? 'Reiniciar' : 'Iniciar'}</button>
        </div>
      </div>`).join('');
  } catch (e) {
    box.innerHTML = '<div class="scard" style="padding:16px">Error al cargar flota de servidores.</div>';
  }
}
