/* ADS-B Display 前端主逻辑：SSE 实时状态 → 地图渲染 + 列表 + 选中轨迹查询 */
(function () {
  'use strict';

  // ------------------------------------------------------------------
  // 全局状态
  // ------------------------------------------------------------------
  const S = {
    mode: null,
    status: null,
    aircraft: new Map(),      // hex -> 最新记录
    selected: null,           // 选中的 hex
    follow: false,
    markers: new Map(),       // hex -> L.Marker
    trails: new Map(),        // hex -> {poly, pts: []}
    selTrack: null,           // 选中的 L.Polyline
    selTrackCount: -1,
    rows: new Map(),          // hex -> DOM row
    viewInit: false,
    search: '',
    receiver: null,
    maxRangeKm: 500,
    sse: null,
    lastUpdate: 0,
    shutdown: false,
  };
  const $ = (id) => document.getElementById(id);
  const PLANE_PATH = 'M0,-46 C2,-46 4,-44 4.5,-40 L6,-18 L44,2 L44,10 L6,-2 L4.5,22 L16,32 L16,38 L0,34 L-16,38 L-16,32 L-4.5,22 L-6,-2 L-44,10 L-44,2 L-6,-18 L-4.5,-40 C-4,-44 -2,-46 0,-46 Z';

  // ------------------------------------------------------------------
  // 地图初始化
  // ------------------------------------------------------------------
  const map = L.map('map', {
    zoomControl: true,
    worldCopyJump: true,
    preferCanvas: false,
    attributionControl: true,
  });

  const streetLayer = L.tileLayer(
    'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
    { subdomains: '1234', maxZoom: 18, className: 'dark-tiles', attribution: '底图 © 高德地图' });
  const satLayer = L.tileLayer(
    'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
    { subdomains: '1234', maxZoom: 18, attribution: '底图 © 高德地图' });
  const satLabelLayer = L.tileLayer(
    'https://webst0{s}.is.autonavi.com/appmaptile?style=8&x={x}&y={y}&z={z}',
    { subdomains: '1234', maxZoom: 18, opacity: 0.9 });

  streetLayer.addTo(map);
  L.control.layers({ '街道地图': streetLayer, '卫星影像': satLayer }, { '卫星路网注记': satLabelLayer }, { collapsed: true }).addTo(map);

  let rxMarker = null, rxRing = null;

  function initReceiverView(lat, lon, maxRangeKm) {
    S.receiver = [lat, lon];
    S.maxRangeKm = maxRangeKm || 500;
    const g = GCJ.wgs2gcj(lat, lon);
    if (!rxMarker) {
      rxMarker = L.marker(g, {
        icon: L.divIcon({ className: 'rx-icon', html: '<div class="rx">📡</div>', iconSize: [24, 24], iconAnchor: [12, 12] }),
        interactive: false, zIndexOffset: -100,
      }).addTo(map);
      rxRing = L.circle(g, {
        radius: S.maxRangeKm * 1000, weight: 1, opacity: 0.3, className: 'rx-ring',
        dashArray: '6 8', fill: false, interactive: false,
      }).addTo(map);
      if (!S.viewInit) { map.setView(g, 8); S.viewInit = true; }
    } else {
      rxMarker.setLatLng(g); rxRing.setLatLng(g); rxRing.setRadius(S.maxRangeKm * 1000);
    }
  }

  // ------------------------------------------------------------------
  // 工具
  // ------------------------------------------------------------------
  function altColor(rec) {
    if (rec.alt_baro === 'ground') return '#90a4ae';
    const a = rec.alt_ft;
    if (a == null) return '#b0bec5';
    if (a <= 10000) return '#00e676';
    if (a <= 20000) return '#ffd600';
    if (a <= 30000) return '#ff9100';
    if (a <= 40000) return '#ff5252';
    return '#d500f9';
  }
  function fmtAlt(rec) {
    if (rec.alt_baro === 'ground') return '地面';
    if (rec.alt_ft == null) return '—';
    return String(Math.round(rec.alt_ft));
  }
  function fmtNum(v, unit, digits) {
    if (v == null || v === '' || Number.isNaN(v)) return '—';
    const n = Number(v);
    return digits != null ? n.toFixed(digits) + (unit || '') : Math.round(n) + (unit || '');
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function post(url) { return fetch(url, { method: 'POST' }).catch(() => {}); }

  // ------------------------------------------------------------------
  // 飞机标记
  // ------------------------------------------------------------------
  function planeIconHtml(color, label, stale) {
    return `<div class="plane-rot${stale ? ' plane-stale' : ''}">` +
      `<svg width="34" height="34" viewBox="-46 -46 92 92">` +
      `<path d="${PLANE_PATH}" fill="${color}" stroke="rgba(0,0,0,.55)" stroke-width="2"/></svg></div>` +
      (label ? `<div class="plane-label">${esc(label)}</div>` : '');
  }

  function upsertMarker(hex, rec, color) {
    if (rec.lat == null || rec.lon == null) return;
    const g = GCJ.wgs2gcj(rec.lat, rec.lon);
    const showLabel = map.getZoom() >= 9;
    const labelText = showLabel ? (rec.flight || hex.toUpperCase()) : '';
    const stale = (rec.age != null && rec.age > 30);
    let m = S.markers.get(hex);
    if (!m) {
      m = L.marker(g, {
        icon: L.divIcon({
          className: 'plane-icon', html: planeIconHtml(color, labelText, stale),
          iconSize: [34, 34], iconAnchor: [17, 17],
        }),
        rotationAngle: 0, keyboard: false, riseOnHover: true,
        zIndexOffset: 400,
      }).addTo(map);
      m.on('click', () => selectHex(hex, { pan: false }));
      S.markers.set(hex, m);
      m._color = color; m._label = labelText; m._stale = stale; m._track = rec.track;
    } else {
      m.setLatLng(g);
      if (m._color !== color || m._label !== labelText || m._stale !== stale) {
        m.setIcon(L.divIcon({
          className: 'plane-icon', html: planeIconHtml(color, labelText, stale),
          iconSize: [34, 34], iconAnchor: [17, 17],
        }));
        m._color = color; m._label = labelText; m._stale = stale;
        if (hex === S.selected) {
          const el = m.getElement();
          if (el) el.classList.add('is-selected');
        }
      }
      m._track = rec.track;
    }
    // 旋转内层元素（不干扰 Leaflet 的定位 transform）
    const el = m.getElement();
    if (el) {
      const rot = el.querySelector('.plane-rot');
      if (rot && rec.track != null) rot.style.transform = `rotate(${rec.track}deg)`;
    }
    // 短尾迹（本地累积，最近 ~25 点）
    let t = S.trails.get(hex);
    if (!t) {
      t = { pts: [], poly: L.polyline([], { color, weight: 1.5, opacity: 0.45, interactive: false }).addTo(map) };
      S.trails.set(hex, t);
    }
    const pts = t.pts;
    const last = pts[pts.length - 1];
    if (!last || Math.abs(last[0] - g[0]) > 1e-6 || Math.abs(last[1] - g[1]) > 1e-6) {
      pts.push(g);
      if (pts.length > 25) pts.shift();
      t.poly.setLatLngs(pts);
    }
    if (t.poly.options.color !== color) t.poly.setStyle({ color });
  }

  function removeMarker(hex) {
    const m = S.markers.get(hex);
    if (m) { map.removeLayer(m); S.markers.delete(hex); }
    const t = S.trails.get(hex);
    if (t) { map.removeLayer(t.poly); S.trails.delete(hex); }
    if (S.selected === hex) {
      if (S.selTrack) { map.removeLayer(S.selTrack); S.selTrack = null; }
      S.selTrackCount = -1;
      S.selected = null; // 清除已移除飞机的选中状态，防止幽灵航迹
      $('ip-callsign').textContent = '（已失联）';
      $('ip-seen').textContent = '—';
    }
  }

  // ------------------------------------------------------------------
  // 选中与轨迹查询
  // ------------------------------------------------------------------
  function selectHex(hex, opts) {
    opts = opts || {};
    if (S.selected !== hex && S.selTrack) {
      map.removeLayer(S.selTrack);
      S.selTrack = null;
    }
    S.selected = hex;
    S.selTrackCount = -1;
    $('infopanel').hidden = false;
    updateSelectionVisual();
    refreshSelectedTrack(true);
    if (opts.pan) {
      const rec = S.aircraft.get(hex);
      if (rec && rec.lat != null) map.panTo(GCJ.wgs2gcj(rec.lat, rec.lon), { animate: true });
    }
  }

  function deselect() {
    S.selected = null;
    S.follow = false;
    $('ip-follow').classList.remove('active');
    $('ip-follow').textContent = '⊘ 跟随追踪';
    $('infopanel').hidden = true;
    if (S.selTrack) { map.removeLayer(S.selTrack); S.selTrack = null; }
    updateSelectionVisual();
  }

  function updateSelectionVisual() {
    for (const [hx, m] of S.markers) {
      const el = m.getElement();
      if (el) el.classList.toggle('is-selected', hx === S.selected);
    }
  }

  function refreshSelectedTrack(force) {
    if (!S.selected) return;
    fetch('/api/track?hex=' + encodeURIComponent(S.selected))
      .then((r) => r.json())
      .then((trk) => {
        if (!trk || trk.count === 0) {
          $('ip-trackpts').textContent = '0';
          if (S.selTrack) { map.removeLayer(S.selTrack); S.selTrack = null; }
          return;
        }
        if (S.selected !== trk.hex) return;
        if (!force && trk.count === S.selTrackCount) return;
        S.selTrackCount = trk.count;
        const latlngs = trk.points
          .filter((p) => p[1] != null && p[2] != null)
          .map((p) => GCJ.wgs2gcj(p[1], p[2]));
        if (!S.selTrack) {
          S.selTrack = L.polyline(latlngs, { className: 'sel-track', weight: 2.5, opacity: 0.9, interactive: false }).addTo(map);
          S.selTrack.bringToBack();
        } else {
          S.selTrack.setLatLngs(latlngs);
        }
        $('ip-trackpts').textContent = String(trk.count);
      })
      .catch(() => {});
  }

  // ------------------------------------------------------------------
  // 信息面板
  // ------------------------------------------------------------------
  function updateInfoPanel() {
    if (!S.selected) return;
    const rec = S.aircraft.get(S.selected);
    if (!rec) {
      $('ip-callsign').textContent = '（已失联）';
      $('ip-seen').textContent = '—';
      return;
    }
    $('ip-callsign').textContent = rec.flight || '——';
    $('ip-hex').textContent = rec.hex.toUpperCase();
    $('ip-squawk').textContent = rec.squawk ? 'SQR ' + rec.squawk : '';
    const cat = $('ip-cat');
    if (rec.category) { cat.hidden = false; cat.textContent = 'CAT ' + rec.category; } else { cat.hidden = true; }
    $('ip-srctype').textContent = (rec.type || '').replace('_icao', '').replace(/_/g, ' ');
    $('ip-altb').textContent = rec.alt_baro === 'ground' ? '地面' : fmtNum(rec.alt_baro, ' ft');
    $('ip-altg').textContent = fmtNum(rec.alt_geom, ' ft');
    $('ip-gs').textContent = fmtNum(rec.gs, ' kt');
    const vr = rec.baro_rate != null ? rec.baro_rate : rec.geom_rate;
    $('ip-vr').textContent = vr != null ? (vr > 0 ? '↑' : vr < 0 ? '↓' : '') + Math.abs(Math.round(vr)) + ' ft/min' : '—';
    $('ip-trk').textContent = fmtNum(rec.track, '°');
    $('ip-hdg').textContent = fmtNum(rec.mag_heading, '°');
    $('ip-mach').textContent = (rec.mach != null ? 'M' + Number(rec.mach).toFixed(2) : '—') + ' / ' + fmtNum(rec.ias, '');
    $('ip-mcp').textContent = fmtNum(rec.nav_altitude_mcp, ' ft');
    $('ip-pos').textContent = (rec.lat != null && rec.lon != null)
      ? rec.lat.toFixed(5) + ', ' + rec.lon.toFixed(5) : '—';
    $('ip-dst').textContent = rec.dist_km != null
      ? rec.dist_km.toFixed(1) + ' km / ' + fmtNum(rec.bearing, '°') : '—';
    $('ip-rssi').textContent = rec.rssi != null ? Number(rec.rssi).toFixed(1) + ' dBFS' : '—';
    $('ip-msgs').textContent = rec.messages != null ? String(rec.messages) : '—';
    $('ip-seen').textContent = rec.age != null ? rec.age.toFixed(1) + ' s 前' : '—';
    // 机型图标旋转与颜色
    const svg = document.querySelector('.ip-plane');
    if (svg) {
      svg.style.transform = `rotate(${rec.track != null ? rec.track : 0}deg)`;
      const p = svg.querySelector('path');
      if (p) p.setAttribute('fill', altColor(rec));
    }
  }

  // ------------------------------------------------------------------
  // 侧栏列表
  // ------------------------------------------------------------------
  function buildRow(hex) {
    const row = document.createElement('div');
    row.className = 'ac-row';
    row.dataset.hex = hex;
    row.innerHTML =
      `<span class="col callsign"><i class="swatch"></i><span class="cs">—</span><span class="hex mono">${hex.toUpperCase()}</span></span>` +
      `<span class="col alt">—</span><span class="col gs">—</span><span class="col trk">—</span>`;
    row.addEventListener('click', () => selectHex(hex, { pan: true }));
    S.rows.set(hex, row);
    return row;
  }

  function updateList() {
    const listEl = $('ac-list');
    const arr = Array.from(S.aircraft.values());
    // 排序：有位置按距离，其次按 hex
    arr.sort((a, b) => {
      const da = a.dist_km == null ? Infinity : a.dist_km;
      const db = b.dist_km == null ? Infinity : b.dist_km;
      return da - db || a.hex.localeCompare(b.hex);
    });
    const q = S.search.trim().toLowerCase();
    let visible = 0;
    for (const rec of arr) {
      const row = S.rows.get(rec.hex) || buildRow(rec.hex);
      row.querySelector('.cs').textContent = rec.flight || '——';
      row.querySelector('.alt').textContent = fmtAlt(rec);
      row.querySelector('.gs').textContent = rec.gs != null ? Math.round(rec.gs) : '—';
      row.querySelector('.trk').textContent = rec.track != null ? Math.round(rec.track) + '°' : '—';
      row.querySelector('.swatch').style.background = altColor(rec);
      row.classList.toggle('selected', rec.hex === S.selected);
      row.classList.toggle('stale', rec.age != null && rec.age > 30);
      const match = !q || rec.hex.toLowerCase().includes(q) ||
        (rec.flight || '').toLowerCase().includes(q);
      row.style.display = match ? '' : 'none';
      if (match) visible++;
      listEl.appendChild(row); // appendChild 会移动节点 → 实现重排序
    }
    // 移除已消失的行
    const alive = new Set(arr.map((r) => r.hex));
    for (const [hx, row] of S.rows) {
      if (!alive.has(hx)) { row.remove(); S.rows.delete(hx); }
    }
    $('list-empty').hidden = visible > 0;
    $('cnt-ac').textContent = String(S.aircraft.size);
  }

  // ------------------------------------------------------------------
  // 状态处理主入口（每秒一次）
  // ------------------------------------------------------------------
  function handleState(st) {
    S.lastUpdate = Date.now();
    // 暂停心跳：只同步暂停指示，不覆盖数据
    if (st.heartbeat && st.status && st.status.display_paused) {
      setConn(true);
      if (S.status) S.status.display_paused = true; // 供按钮点击判定当前暂停态
      updatePausedBanner(true);
      $('st-upd').textContent = '映射已暂停（后台采集中）';
      return;
    }
    const prevMode = S.mode;
    S.status = st.status;
    S.mode = st.status.mode;
    if (!S.receiver) {
      initReceiverView(st.status.receiver.lat, st.status.receiver.lon, st.status.receiver.max_range_km);
    }

    if (prevMode !== S.mode) {
      if (prevMode !== null) deselect(); // 模式切换后旧选择已失效
      const badge = $('mode-badge');
      badge.textContent = S.mode === 'live' ? 'LIVE' : 'REPLAY';
      badge.className = 'badge ' + (S.mode === 'live' ? 'badge-live' : 'badge-replay');
      $('replay-ctrl').hidden = S.mode !== 'replay';
      $('dataset-badge').textContent = st.status.dataset ? '数据集 ' + st.status.dataset : '';
      $('st-mode').textContent = S.mode === 'live' ? '实时采集 (RTL-SDR + readsb)' : '数据集回放';
    }

    // 暂停指示（仅按钮图标切换，无横幅）
    if (S.mode === 'live') updateWarmup(st.status.warmup);
    updatePausedBanner(st.status.display_paused === true);

    // 活动集
    const alive = new Set();
    for (const rec of st.aircraft) {
      alive.add(rec.hex);
      S.aircraft.set(rec.hex, rec);
      upsertMarker(rec.hex, rec, altColor(rec));
    }
    for (const hx of Array.from(S.aircraft.keys())) {
      if (!alive.has(hx)) { S.aircraft.delete(hx); removeMarker(hx); }
    }

    updateList();
    updateInfoPanel();
    if (S.selected) refreshSelectedTrack(false);
    if (S.follow && S.selected) {
      const rec = S.aircraft.get(S.selected);
      if (rec && rec.lat != null) map.panTo(GCJ.wgs2gcj(rec.lat, rec.lon), { animate: true });
    }
    updateStatusbar();
    updateReplayCtrl();
  }

  function updateStatusbar() {
    const st = S.status;
    if (!st) return;
    const src = st.source || {};
    if (st.mode === 'live') {
      let txt = `readsb ${src.running ? '运行中' : '已停止'} · 报文 ${src.messages || 0}`;
      if (src.restarts) txt += ` · 重启 ${src.restarts} 次`;
      if (src.last_error) txt += ` · ${src.last_error}`;
      $('st-src').textContent = txt;
      $('st-src').style.color = src.running ? '' : 'var(--err)';
      // 报文计数：后端实际采集的 DF17 且 CRC 校验通过报文
      const ms = st.msgstats || {};
      $('cnt-msg').textContent = String(ms.messages_df17 != null ? ms.messages_df17 : 0);
    } else {
      $('st-src').textContent = `${src.dataset || ''} · ${src.batch_index || 0}/${src.total_batches || 0} 批 · ${src.speed || 1}×` +
        (src.paused ? ' · 已暂停' : '');
      $('cnt-msg').textContent = '—';
    }
    const ago = Math.round((Date.now() - S.lastUpdate) / 1000);
    $('st-upd').textContent = ago <= 2 ? '刚刚更新' : `${ago} 秒前更新`;
  }

  // ------------------------------------------------------------------
  // 预热遮罩
  // ------------------------------------------------------------------
  const WU_LABELS = {
    checking_hardware: '正在检查物理硬件',
    initializing: '正在初始化启动脚本',
    collecting: '正在收集数据',
    ready: '进入实时显示',
  };
  let wuVisible = false;
  let wuDoneAt = 0;

  function updateWarmup(wu) {
    const overlay = $('warmup-overlay');
    if (!wu) { overlay.hidden = true; wuVisible = false; return; }
    const items = document.querySelectorAll('#wu-steps li');
    const idx = { checking_hardware: 0, initializing: 1, collecting: 2, ready: 3 }[wu.phase] ?? 0;
    items.forEach((li, i) => {
      li.classList.toggle('done', i < idx || wu.phase === 'ready');
      li.classList.toggle('active', i === idx && wu.phase !== 'ready');
      const em = li.querySelector('em');
      if (i < idx && wu.steps[i]) em.textContent = wu.steps[i].seconds + 's';
      else if (i === idx && wu.phase !== 'ready') em.textContent = Math.round(wu.phase_elapsed) + 's';
      else em.textContent = i === 3 && wu.phase === 'ready' ? '✔' : '';
    });
    const det = $('wu-detail');
    if (wu.error) {
      det.textContent = '⚠ ' + wu.error;
      det.className = 'err';
      $('wu-skip').hidden = false;
      $('wu-skip').textContent = '仍要进入显示 →';
    } else {
      det.textContent = wu.detail || WU_LABELS[wu.phase] || '';
      det.className = '';
      // collecting 阶段超过 20 秒显示跳过按钮（室内信号弱时航班出现慢）
      $('wu-skip').hidden = !(wu.phase === 'collecting' && wu.phase_elapsed > 20);
    }
    if (wu.phase === 'ready') {
      // 就绪后保留 1.2s 过渡再淡出
      if (!wuDoneAt) {
        wuDoneAt = Date.now();
        setTimeout(() => {
          overlay.hidden = true;
          wuVisible = false;
        }, 1200);
      }
      if (Date.now() - wuDoneAt > 1200) {
        overlay.hidden = true;
        wuVisible = false;
      }
    } else {
      wuDoneAt = 0;
      overlay.hidden = false;
      wuVisible = true;
    }
  }

  $('wu-skip').addEventListener('click', () => post('/api/warmup/skip'));

  // ------------------------------------------------------------------
  // 暂停显示 / 终止系统
  // ------------------------------------------------------------------
  // 无横幅：仅按钮图标/文字切换（⏸ 暂停显示 ↔ ▶ 恢复显示）
  function updatePausedBanner(paused) {
    const btn = $('btn-pause');
    if (S.mode === 'replay') {
      const srcPaused = S.status && S.status.source && S.status.source.paused;
      btn.classList.toggle('paused', !!srcPaused);
      btn.textContent = srcPaused ? '▶ 恢复回放' : '⏸ 暂停回放';
      btn.title = srcPaused ? '恢复回放进度' : '暂停回放进度';
    } else {
      btn.classList.toggle('paused', paused);
      btn.textContent = paused ? '▶ 恢复显示' : '⏸ 暂停显示';
      btn.title = paused ? '恢复前端映射（后台采集未中断）' : '暂停前端映射（后台继续采集）';
    }
  }

  $('btn-pause').addEventListener('click', () => {
    if (S.mode === 'replay') {
      const paused = S.status && S.status.source && S.status.source.paused;
      post(paused ? '/api/replay/resume' : '/api/replay/pause');
    } else {
      const paused = S.status && S.status.display_paused;
      post(paused ? '/api/display/resume' : '/api/display/pause');
    }
  });

  $('btn-shutdown').addEventListener('click', () => { $('shutdown-modal').hidden = false; });
  $('sd-cancel').addEventListener('click', () => { $('shutdown-modal').hidden = true; });
  $('sd-confirm').addEventListener('click', async () => {
    $('sd-confirm').disabled = true;
    $('sd-confirm').textContent = '正在终止…';
    S.shutdown = true;
    await post('/api/system/shutdown');
    // 等待后端退出、连接断开后显示收尾页
    setTimeout(() => {
      $('shutdown-modal').hidden = true;
      $('shutdown-done').hidden = false;
      $('sse-dot').className = 'dot dot-off';
      $('sse-text').textContent = '已终止';
    }, 1500);
  });

  // ------------------------------------------------------------------
  // 回放控制
  // ------------------------------------------------------------------
  function updateReplayCtrl() {
    const src = S.status && S.status.source;
    if (!src || S.mode !== 'replay') return;
    $('rp-toggle').textContent = src.paused ? '▶' : '⏸';
    const seek = $('rp-seek');
    seek.max = Math.max(0, (src.total_batches || 1) - 1);
    if (document.activeElement !== seek) seek.value = src.batch_index || 0;
    $('rp-progress').textContent = `${src.batch_index || 0}/${src.total_batches || 0}`;
    if (String(src.speed || 1) !== $('rp-speed').value) {
      $('rp-speed').value = String(src.speed || 1);
    }
  }

  $('rp-toggle').addEventListener('click', () => {
    const paused = S.status && S.status.source && S.status.source.paused;
    post(paused ? '/api/replay/resume' : '/api/replay/pause');
  });
  $('rp-seek').addEventListener('change', (e) => {
    post('/api/replay/seek?index=' + e.target.value);
  });
  $('rp-speed').addEventListener('change', (e) => {
    post('/api/replay/speed?value=' + e.target.value);
  });

  // ------------------------------------------------------------------
  // 搜索
  // ------------------------------------------------------------------
  const searchInput = $('search');
  searchInput.addEventListener('input', () => {
    S.search = searchInput.value;
    $('search-clear').hidden = !S.search;
    updateList();
  });
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { searchInput.value = ''; S.search = ''; $('search-clear').hidden = true; updateList(); return; }
    if (e.key !== 'Enter') return;
    const q = S.search.trim().toLowerCase();
    if (!q) return;
    // 精确 hex 优先，其次包含匹配航班号/hex（与列表过滤规则保持一致）
    let hit = null;
    for (const rec of S.aircraft.values()) {
      if (rec.hex.toLowerCase() === q) { hit = rec; break; }
      if (!hit && ((rec.flight || '').toLowerCase().includes(q) || rec.hex.toLowerCase().includes(q))) hit = rec;
    }
    if (hit) { selectHex(hit.hex, { pan: true }); searchInput.blur(); }
  });
  $('search-clear').addEventListener('click', () => {
    searchInput.value = ''; S.search = ''; $('search-clear').hidden = true; updateList();
  });

  // ------------------------------------------------------------------
  // 信息面板按钮
  // ------------------------------------------------------------------
  $('ip-close').addEventListener('click', deselect);
  $('ip-follow').addEventListener('click', () => {
    S.follow = !S.follow;
    $('ip-follow').classList.toggle('active', S.follow);
    $('ip-follow').textContent = S.follow ? '✔ 跟随中(点击取消)' : '⊘ 跟随追踪';
    if (S.follow && S.selected) {
      const rec = S.aircraft.get(S.selected);
      if (rec && rec.lat != null) map.panTo(GCJ.wgs2gcj(rec.lat, rec.lon));
    }
  });
  $('ip-center').addEventListener('click', () => {
    if (!S.selected) return;
    const rec = S.aircraft.get(S.selected);
    if (rec && rec.lat != null) map.setView(GCJ.wgs2gcj(rec.lat, rec.lon), Math.max(map.getZoom(), 9));
  });

  // ------------------------------------------------------------------
  // 连接（SSE，断线自动重连；多次失败退化为 1s 轮询）
  // ------------------------------------------------------------------
  function setConn(ok) {
    if (S.shutdown) return; // 已终止，保持收尾状态
    $('sse-dot').className = 'dot ' + (ok ? 'dot-on' : 'dot-off');
    $('sse-text').textContent = ok ? '实时连接' : '连接断开，重试中…';
  }

  let pollTimer = null;
  function startPolling() {
    if (pollTimer || S.shutdown) return;
    console.warn('SSE 不可用，退化为轮询模式');
    pollTimer = setInterval(async () => {
      try {
        const r = await fetch('/api/state');
        const st = await r.json();
        setConn(true);
        handleState(st);
      } catch (e) { setConn(false); }
    }, 1000);
  }

  function connectSSE() {
    if (S.shutdown) return;
    const es = new EventSource('/api/stream');
    S.sse = es;
    es.onopen = () => {
      setConn(true);
      failCount = 0;
    };
    es.onmessage = (ev) => {
      try {
        setConn(true);
        failCount = 0;
        handleState(JSON.parse(ev.data));
      } catch (e) { console.error('bad sse payload', e); }
    };
    es.onerror = () => {
      setConn(false);
      if (S.shutdown) { es.close(); return; }
      failCount++;
      if (failCount >= 3) {
        es.close();
        startPolling();
      } else if (es.readyState === EventSource.CLOSED) {
        setTimeout(connectSSE, 2000);
      }
    };
  }
  let failCount = 0;

  // ------------------------------------------------------------------
  // 主题切换（浅色 / 深色，localStorage 持久化）
  // ------------------------------------------------------------------
  $('btn-theme').addEventListener('click', () => {
    const root = document.documentElement;
    const next = root.dataset.theme === 'light' ? 'dark' : 'light';
    root.dataset.theme = next;
    try { localStorage.setItem('adsb-theme', next); } catch (e) { /* 隐私模式忽略 */ }
  });

  // ------------------------------------------------------------------
  // 时钟（UTC / 北京时间点击切换）与初始元数据
  // ------------------------------------------------------------------
  const clockBox = $('clock-box');
  let clockUtc = true; // true: UTC；false: 北京时间（Asia/Shanghai, UTC+8）
  clockBox.addEventListener('click', () => {
    clockUtc = !clockUtc;
    $('clock-label').textContent = clockUtc ? 'UTC' : '北京时间';
    $('clock').textContent = renderClock(); // 立即刷新，避免最长 0.5s 延迟
  });
  function renderClock() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    if (clockUtc) {
      return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds());
    }
    return pad((d.getUTCHours() + 8) % 24) + ':' +
      pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds());
  }
  setInterval(() => { $('clock').textContent = renderClock(); }, 500);

  map.on('zoomend', () => {
    // 缩放级别变化时刷新标签显示
    const z = map.getZoom();
    const show = z >= 9;
    for (const [, m] of S.markers) {
      const el = m.getElement();
      if (!el) continue;
      const lab = el.querySelector('.plane-label');
      if (lab) lab.style.display = show ? '' : 'none';
    }
  });

  fetch('/api/meta').then((r) => r.json()).then((meta) => {
    if (meta.receiver) initReceiverView(meta.receiver.lat, meta.receiver.lon, meta.receiver.max_range_km);
  }).catch(() => {});

  connectSSE();
})();
