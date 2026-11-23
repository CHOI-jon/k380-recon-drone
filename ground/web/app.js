// =========================================================
// 핵심: 지도 · 텔레메트리 · 객체 목록/갤러리 · 서버 연결
// 부가 기능(성능/구역/경보/정확도/보고서)은 features.js가 App.on()으로 붙는다
// =========================================================
const $ = id => document.getElementById(id);
const CLS_KO = { person: "사람", vehicle: "차량" };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const App = {
  map: null, cfg: null, objects: [], state: null,
  handlers: [], on(fn) { this.handlers.push(fn); },
  mapClick: null,            // 기능별 지도 클릭 가로채기 (구역 그리기 / 기준점 찍기)
  focusObject(id) {
    const L_ = objLayers[id];
    if (!L_) return;
    followDrone = false; $("followDrone").checked = false;
    this.map.setView(L_.marker.getLatLng(), 20);
    L_.marker.openPopup();
    $("map").scrollIntoView({ behavior: "smooth", block: "center" });
  },
  api(path, opts = {}) {
    return fetch(path, { headers: { "Content-Type": "application/json" }, ...opts })
      .then(async r => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.status); return j; });
  },
};

let droneMarker, dronePath, followDrone = true, firstFix = true;
const objLayers = {};

// ---------------- 지도
function initMap(center) {
  const map = (App.map = L.map("map", { zoomControl: true, doubleClickZoom: false }).setView(center, 18));
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 21, maxNativeZoom: 19, attribution: "Tiles © Esri"
  }).addTo(map);
  droneMarker = L.marker(center, {
    icon: L.divIcon({ className: "", html: '<div class="drone-icon"><div class="arrow"><svg viewBox="0 0 48 48" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg"><g stroke="#fff" stroke-linecap="round"><line x1="12" y1="12" x2="36" y2="36" stroke-width="4"/><line x1="36" y1="12" x2="12" y2="36" stroke-width="4"/></g><g fill="rgba(255,255,255,0.28)" stroke="#fff" stroke-width="2"><circle cx="11" cy="11" r="8"/><circle cx="37" cy="11" r="8"/><circle cx="11" cy="37" r="8"/><circle cx="37" cy="37" r="8"/></g><g fill="#fff"><circle cx="11" cy="11" r="2"/><circle cx="37" cy="11" r="2"/><circle cx="11" cy="37" r="2"/><circle cx="37" cy="37" r="2"/></g><rect x="17" y="15" width="14" height="18" rx="5" fill="#fff"/><path d="M24 17.5l4 6h-8z" fill="#0f172a"/></svg></div></div>', iconSize: [46, 46], iconAnchor: [23, 23] }),
    zIndexOffset: 1000, title: "드론"
  }).addTo(map);
  dronePath = L.polyline([], { color: "#facc15", weight: 2, opacity: 0.7 }).addTo(map);
  map.on("dragstart", () => { followDrone = false; $("followDrone").checked = false; });
  map.on("click", e => App.mapClick && App.mapClick(e.latlng));
  $("followDrone").addEventListener("change", e => (followDrone = e.target.checked));
}

function objIcon(o) {
  return L.divIcon({
    className: "",
    html: `<div class="obj-icon ${o.cls}${o.active ? " active" : ""}${o.zones.length ? " inzone" : ""}${o.left ? " left" : ""}">${o.cls === "person" ? "P" : "V"}</div>`,
    iconSize: [26, 26], iconAnchor: [13, 13]
  });
}

function popupHtml(o) {
  return `${o.snap ? `<img class="pop-img" src="${o.snap}" alt="${o.id} 스냅샷">` : ""}
    <b>${o.id}</b> ${CLS_KO[o.cls]} · 신뢰도 ${o.conf}<br>${o.lat.toFixed(6)}, ${o.lon.toFixed(6)}<br>
    오차 ±${o.err} m · ${o.hits}회 탐지<br>최근 ${o.last_seen}`;
}

function updateDrone(s) {
  if (s.lat == null || s.lon == null) return;
  const p = [s.lat, s.lon];
  droneMarker.setLatLng(p);
  const el = droneMarker.getElement();
  if (el && s.heading != null) el.querySelector(".arrow").style.transform = `rotate(${s.heading}deg)`;
  const pts = dronePath.getLatLngs();
  const last = pts[pts.length - 1];
  if (!last || App.map.distance(last, p) > 0.5) {
    dronePath.addLatLng(p);
    if (pts.length > 2000) dronePath.setLatLngs(pts.slice(-1500));
  }
  if (firstFix) { App.map.setView(p, 19); firstFix = false; }
  else if (followDrone) App.map.panTo(p, { animate: false });
}

function updateObjectMarkers(objects) {
  const hl = $("hideLeft");
  if (hl && hl.checked) objects = objects.filter(o => !o.left);     // 이탈 확인된 객체는 지도에서 숨김 (목록에는 남김)
  const seen = new Set();
  for (const o of objects) {
    seen.add(o.id);
    const ll = [o.lat, o.lon];
    const color = o.cls === "person" ? "#f43f5e" : "#38bdf8";
    const key = `${o.active}|${o.zones.length}|${!!o.left}`;
    let L_ = objLayers[o.id];
    if (!L_) {
      L_ = objLayers[o.id] = {
        marker: L.marker(ll, { icon: objIcon(o), title: o.id }).addTo(App.map).bindPopup(popupHtml(o), { maxWidth: 220 }),
        ring: L.circle(ll, { radius: o.err, color, weight: 1, fillOpacity: 0.08, interactive: false }).addTo(App.map),
        key, snap: o.snap
      };
    } else {
      L_.marker.setLatLng(ll);
      L_.ring.setLatLng(ll).setRadius(o.err);
      if (L_.key !== key) { L_.marker.setIcon(objIcon(o)); L_.key = key; }
      if (L_.marker.isPopupOpen()) { if (L_.snap !== o.snap) L_.marker.setPopupContent(popupHtml(o)); }
      else L_.marker.setPopupContent(popupHtml(o));
      L_.snap = o.snap;
    }
  }
  for (const id of Object.keys(objLayers)) {
    if (!seen.has(id)) {
      App.map.removeLayer(objLayers[id].marker);
      App.map.removeLayer(objLayers[id].ring);
      delete objLayers[id];
    }
  }
}

// ---------------- 텔레메트리
const fmt = (v, unit, d = 1) => (v == null ? "-" : `${Number(v).toFixed(d)}${unit}`);
function setPill(el, ok, onText, offText) {
  el.textContent = ok ? onText : offText;
  el.className = "status-pill " + (ok ? "on" : "off");
}
function updateTelemetry(s) {
  $("altitude").textContent = fmt(s.altitude, " m");
  $("speed").textContent = fmt(s.speed, " m/s");
  $("battery").textContent = s.battery == null ? fmt(s.voltage, " V", 2) : `${s.battery}%` + (s.voltage ? ` (${s.voltage}V)` : "");
  $("gps").textContent = s.gps;
  $("lat").textContent = s.lat == null ? "-" : s.lat.toFixed(6);
  $("lon").textContent = s.lon == null ? "-" : s.lon.toFixed(6);
  $("heading").textContent = fmt(s.heading, "°", 0);
  $("attitude").textContent = s.roll == null ? "-" : `${s.roll.toFixed(0)}° / ${s.pitch.toFixed(0)}°`;
  $("flightMode").textContent = `${s.mode}${s.armed ? " · ARMED" : ""}`;
  $("fpsText").textContent = `FPS ${s.fps}`;
  $("personCount").textContent = s.detections.person;
  $("vehicleCount").textContent = s.detections.vehicle;
  setPill($("fcStatus"), s.link.fc, "FC 연결", "FC 끊김");
  setPill($("piStatus"), s.link.pi, "PI 연결", "PI 끊김");
  $("benchBanner").hidden = !s.bench;
}

// ---------------- 객체 목록 + 갤러리
let tableSig = "", gallerySig = "";
function updateObjectTable(objects) {
  const nP = objects.filter(o => o.cls === "person").length;
  $("objTotal").textContent = `${nP} / ${objects.length - nP}`;
  if (objects.length) $("lastDetectionTime").textContent =
    `최근 탐지: ${objects.reduce((a, o) => (o.last_seen > a ? o.last_seen : a), "")}`;
  const zname = Object.fromEntries((App.zones || []).map(z => [z.id, z.name]));

  const sig = JSON.stringify(objects) + JSON.stringify(zname);
  if (sig === tableSig) return;
  tableSig = sig;
  const tbody = $("objectTable");
  if (!objects.length) {
    tbody.innerHTML = `<tr><td colspan="12" class="muted">아직 탐지된 객체가 없습니다.</td></tr>`;
  } else {
    const sorted = [...objects].sort((a, b) => b.last_seen.localeCompare(a.last_seen) || a.id.localeCompare(b.id));
    tbody.innerHTML = sorted.map(o => `
      <tr data-id="${o.id}" class="${o.active ? "row-active" : ""}${o.zones.length ? " row-zone" : ""}">
        <td>${o.snap ? `<img class="thumb" src="${o.snap}" alt="" loading="lazy">` : `<span class="thumb empty"></span>`}</td>
        <td><b>${o.id}</b></td>
        <td><span class="tag ${o.cls}">${CLS_KO[o.cls]}</span></td>
        <td>${o.conf.toFixed(2)}</td>
        <td>${o.lat.toFixed(6)}</td>
        <td>${o.lon.toFixed(6)}</td>
        <td>±${o.err}</td>
        <td>${o.hits}</td>
        <td>${o.first_seen}</td>
        <td>${o.last_seen}</td>
        <td>${o.zones.map(z => `<span class="tag zone">⚠ ${esc(zname[z] || z)}</span>`).join(" ") || '<span class="muted">-</span>'}</td>
        <td>${o.active ? '<span class="live">● 추적 중</span>' : o.left ? '<span class="gone">✕ 이탈 확인</span>' : '<span class="muted">○ 시야 밖</span>'}</td>
      </tr>`).join("");
  }
  updateGallery(objects, zname);
}

function updateGallery(objects, zname) {
  const withSnap = objects.filter(o => o.snap).sort((a, b) => a.id.localeCompare(b.id));
  const sig = withSnap.map(o => o.id + o.snap + o.active + o.zones.join()).join("|");
  if (sig === gallerySig) return;
  gallerySig = sig;
  const g = $("gallery");
  if (!withSnap.length) { g.innerHTML = `<p class="muted">스냅샷이 없습니다. 탐지된 객체의 가장 선명한 순간이 여기에 모입니다.</p>`; return; }
  g.innerHTML = withSnap.map(o => `
    <figure class="snap-card${o.zones.length ? " zone" : ""}" data-id="${o.id}" tabindex="0">
      <img src="${o.snap}" alt="${o.id} ${CLS_KO[o.cls]} 스냅샷" loading="lazy">
      <figcaption>
        <div><b>${o.id}</b> <span class="tag ${o.cls}">${CLS_KO[o.cls]}</span> ${o.active ? '<span class="live">●</span>' : ""}</div>
        <div class="small">신뢰도 ${o.conf.toFixed(2)} · ${o.snap_time || o.first_seen}</div>
        <div class="small muted">${o.lat.toFixed(6)}, ${o.lon.toFixed(6)}</div>
        ${o.zones.map(z => `<span class="tag zone">⚠ ${esc(zname[z] || z)}</span>`).join(" ")}
      </figcaption>
    </figure>`).join("");
}

$("objectTable").addEventListener("click", e => {
  const tr = e.target.closest("tr[data-id]");
  if (tr) App.focusObject(tr.dataset.id);
});
$("gallery").addEventListener("click", e => {
  const c = e.target.closest("[data-id]");
  if (c) App.focusObject(c.dataset.id);
});
$("gallery").addEventListener("keydown", e => {
  const c = e.target.closest("[data-id]");
  if (c && e.key === "Enter") App.focusObject(c.dataset.id);
});
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === t));
  document.querySelectorAll(".tab-body").forEach(b => (b.hidden = b.id !== t.dataset.tab));
}));

function downloadText(name, text, type = "text/plain") {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + text], { type: type + ";charset=utf-8" }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
const stamp = () => new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");

$("exportBtn").addEventListener("click", () => {
  const head = "id,class,conf,lat,lon,err_m,hits,first_seen,last_seen,zones\n";
  const body = App.objects.map(o => [o.id, o.cls, o.conf, o.lat, o.lon, o.err, o.hits, o.first_seen, o.last_seen, o.zones.join(" ")].join(",")).join("\n");
  downloadText(`detected_objects_${stamp()}.csv`, head + body, "text/csv");
});

// ---------------- 서버 연결
let socket;
function connect() {
  socket = new WebSocket(`ws://${location.host}/ws`);
  socket.onopen = () => setPill($("connectionStatus"), true, "서버 연결", "");
  socket.onmessage = ev => {
    let s = JSON.parse(ev.data);
    if (s.type !== "state") return;
    if (App.replay) s = App.replay.patch(s);   // 녹화 영상 리플레이 중이면 영상 시각의 데이터로 교체 (replay.js)
    App.state = s;
    App.objects = s.objects;
    updateTelemetry(s);
    updateDrone(s);
    updateObjectMarkers(s.objects);
    updateObjectTable(s.objects);
    App.handlers.forEach(fn => { try { fn(s); } catch (e) { console.error(e); } });
  };
  socket.onclose = () => {
    setPill($("connectionStatus"), false, "", "서버 끊김 — 재연결 중");
    setTimeout(connect, 2000);
  };
}

$("clearObjBtn").addEventListener("click", () => {
  if (socket && socket.readyState === 1) socket.send(JSON.stringify({ cmd: "clear_objects" }));
  dronePath.setLatLngs([]);
});

// ---------------- 영상 (파이 MJPEG 직접 수신 — 박스는 파이에서 그려서 보냄)
function startVideo(url) {
  const img = $("droneVideo"), empty = $("videoEmpty");
  if (!url) { empty.textContent = "시뮬레이션 모드 — 영상 없음"; return; }
  img.onload = () => (empty.hidden = true);
  setInterval(() => { if (img.naturalWidth > 0) empty.hidden = true; }, 1000);
  img.onerror = () => {
    empty.hidden = false;
    empty.textContent = `영상 연결 실패 (${url}) — 3초 후 재시도`;
    setTimeout(() => (img.src = url + "?t=" + Date.now()), 3000);
  };
  img.src = url;
}

App.ready = fetch("/config.json").then(r => r.json()).then(cfg => {
  App.cfg = cfg;
  initMap(cfg.home);
  startVideo(cfg.video_url);
  connect();
  return cfg;
});
