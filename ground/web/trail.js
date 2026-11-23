// 탐지 객체 이동 경로: 목록(또는 지도 마커·스냅샷)에서 객체를 누르면 처음 탐지 위치 → 마지막 탐지 위치까지
// 탐지 좌표를 시간 순서로 이어 지도에 표시한다. 다른 곳을 누르면 원래 상태로 돌아간다.
(function () {
  if (typeof App === "undefined" || !App.ready) return;
  const CLR = { person: "#f43f5e", vehicle: "#38bdf8" };
  let sel = null, layer = null, prevFollow = null, lastRefresh = 0, info = null;

  const pin = (cls, text) => L.divIcon({ className: "", html: `<div class="trail-pin ${cls}">${text}</div>`, iconSize: [40, 20], iconAnchor: [20, 26] });

  async function load(id) {
    if (App.replay && App.replay.trail) return App.replay.trail(id);
    try { return (await App.api(`/api/objects/${encodeURIComponent(id)}/trail`)).trail || []; } catch (e) { return []; }
  }

  function draw(id, pts, fit) {
    if (layer) { App.map.removeLayer(layer); layer = null; }
    if (!pts.length) { showInfo(`${id} — 경로 기록이 없습니다`); return; }
    const color = CLR[id.startsWith("P") ? "person" : "vehicle"];
    const ll = pts.map((p) => [p.lat, p.lon]);
    layer = L.layerGroup().addTo(App.map);
    L.polyline(ll, { color, weight: 3, opacity: 0.95, interactive: false }).addTo(layer);
    ll.forEach((p) => L.circleMarker(p, { radius: 3, color: "#fff", weight: 1, fillColor: color, fillOpacity: 1, interactive: false }).addTo(layer));
    L.marker(ll[0], { icon: pin("start", "시작"), interactive: false, zIndexOffset: 900 }).addTo(layer);
    if (ll.length > 1) L.marker(ll[ll.length - 1], { icon: pin("end", "마지막"), interactive: false, zIndexOffset: 900 }).addTo(layer);
    const span = App.map.distance(ll[0], ll[ll.length - 1]);
    let far = 0;
    for (const p of ll) far = Math.max(far, App.map.distance(ll[0], p));
    showInfo(`<b>${id}</b> 탐지 위치 경로 · ${pts.length}회 탐지 · 처음 ${pts[0].time} → 마지막 ${pts[pts.length - 1].time}` +
             ` · 처음↔마지막 ${span.toFixed(1)} m · 최대 이동 범위 ${far.toFixed(1)} m <span class="muted">· 다른 곳을 누르면 해제 (Esc)</span>`);
    dim();
    if (fit) App.map.fitBounds(L.latLngBounds(ll).pad(0.4), { maxZoom: 21, animate: false });
  }

  // 선택한 객체 외 마커는 흐리게, 선택한 객체 마커·오차원은 경로가 보이도록 투명하게
  function dim() {
    for (const [id, L_] of Object.entries(objLayers)) {
      const on = sel === null;
      L_.marker.setOpacity(on ? 1 : id === sel ? 0.35 : 0.25);
      L_.ring.setStyle({ opacity: on ? 1 : 0.15, fillOpacity: on ? 0.08 : 0.02 });
    }
  }

  function showInfo(html) {
    if (!info) {
      info = document.createElement("div");
      info.className = "trail-info";
      document.querySelector(".map-wrap").appendChild(info);
    }
    info.innerHTML = html;
    info.hidden = false;
  }

  function highlight() {
    document.querySelectorAll("#objectTable tr[data-id]").forEach((tr) => tr.classList.toggle("row-selected", tr.dataset.id === sel));
  }

  async function select(id) {
    if (sel === id) return;
    if (prevFollow === null) prevFollow = $("followDrone").checked;
    sel = id;
    followDrone = false; $("followDrone").checked = false;
    highlight();
    const pts = await load(id);
    if (sel === id) draw(id, pts, true);
  }

  function clear() {
    if (!sel) return;
    sel = null;
    if (layer) { App.map.removeLayer(layer); layer = null; }
    if (info) info.hidden = true;
    App.map.closePopup();
    dim();
    highlight();
    if (prevFollow) { followDrone = true; $("followDrone").checked = true; }
    prevFollow = null;
  }

  const orig = App.focusObject.bind(App);
  App.focusObject = function (id) {
    if (prevFollow === null) prevFollow = $("followDrone").checked;   // 원래 '드론 따라가기' 상태 기억
    orig(id);
    App.map.closePopup();   // 경로가 가려지지 않게 (정보는 지도 아래 안내줄에 표시)
    select(id);
  };

  App.ready.then(() => {
    App.map.on("popupopen", (e) => {
      const t = e.popup && e.popup._source && e.popup._source.options && e.popup._source.options.title;
      if (t && /^[PV]-\d+$/.test(t)) { if (prevFollow === null) prevFollow = $("followDrone").checked; select(t); }
    });
    App.map.on("click", clear);
  });
  document.addEventListener("click", (e) => {
    if (!sel) return;
    if (e.target.closest("tr[data-id], .snap-card, .leaflet-marker-icon, .leaflet-popup, .trail-info, #map, .tabs")) return;
    clear();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") clear(); });

  // 표가 다시 그려져도 선택 유지, 경로는 새 탐지가 쌓이면 1초마다 갱신
  App.on(() => {
    highlight();
    if (sel) dim();
    const now = Date.now();
    if (sel && now - lastRefresh > 1000) {
      lastRefresh = now;
      const id = sel;
      load(id).then((pts) => { if (sel === id) draw(id, pts, false); });
    }
  });
})();
