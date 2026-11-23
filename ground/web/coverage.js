// 정찰 범위 — 카메라가 지금까지 훑은 땅을 지도에 칠하고 면적·비행 거리를 계산
// 2 m 격자 칸마다 "처음 촬영된 시각"을 기록 → 리플레이는 재생 시각까지, 실시간은 지금까지 칠함
// App.coverageStats() : {area_m2, path_m, dur_s}   App.coverageReport() : 보고서 그림용 요약
(function () {
  if (typeof App === "undefined" || !App.ready) return;
  const CELL = 2;                         // m
  const $ = (id) => document.getElementById(id);
  let grid = new Map();                   // "i,j" → 처음 촬영 시각 (리플레이: 영상 초, 실시간: epoch 초)
  let origin = null, mE = 111320;
  let pathPts = [];                       // [t, lat, lon]
  let mode = null, overlay = null, lastRender = 0, lastLiveFp = 0, renderedKey = "";
  let shown = true;
  try { shown = localStorage.getItem("dash.coverage") !== "0"; } catch (e) {}

  function reset(o) {
    grid = new Map(); pathPts = []; origin = o || null; renderedKey = "";
    if (origin) mE = 111320 * Math.cos((origin[0] * Math.PI) / 180);
    if (overlay) { App.map.removeLayer(overlay); overlay = null; }
  }
  const toXY = (p) => [(p[1] - origin[1]) * mE, (p[0] - origin[0]) * 111320];   // [동, 북] m

  // 볼록 사각형 안의 격자 칸 채우기
  function addPoly(poly, t) {
    if (!origin) { origin = poly[0]; mE = 111320 * Math.cos((origin[0] * Math.PI) / 180); }
    const q = poly.map(toXY);
    const xs = q.map((p) => p[0]), ys = q.map((p) => p[1]);
    const i0 = Math.floor(Math.min(...xs) / CELL), i1 = Math.floor(Math.max(...xs) / CELL);
    const j0 = Math.floor(Math.min(...ys) / CELL), j1 = Math.floor(Math.max(...ys) / CELL);
    if ((i1 - i0) * (j1 - j0) > 200000) return;     // 비정상적으로 큰 범위(자세 오류)는 버림
    let sign = 0;
    for (let k = 0; k < 4; k++) {
      const a = q[k], b = q[(k + 1) % 4];
      sign += (b[0] - a[0]) * (b[1] + a[1]);
    }
    const inside = (x, y) => {
      for (let k = 0; k < 4; k++) {
        const a = q[k], b = q[(k + 1) % 4];
        const cr = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]);
        if (sign > 0 ? cr > 0 : cr < 0) return false;
      }
      return true;
    };
    for (let i = i0; i <= i1; i++) for (let j = j0; j <= j1; j++) {
      const key = i + "," + j;
      if (grid.has(key)) continue;
      if (inside((i + 0.5) * CELL, (j + 0.5) * CELL)) grid.set(key, t);
    }
  }

  function pathLen(upto) {
    let d = 0, prev = null;
    for (const p of pathPts) {
      if (p[0] > upto) break;
      if (prev) d += Math.hypot((p[1] - prev[1]) * 111320, (p[2] - prev[2]) * mE);
      prev = p;
    }
    return d;
  }

  function nowT() {
    if (mode === "replay") { const v = document.querySelector("video.demo-video"); return v ? v.currentTime : 0; }
    return Date.now() / 1000;
  }

  function cellsUpto(t) {
    const out = [];
    for (const [k, t0] of grid) if (t0 <= t) out.push(k);
    return out;
  }

  // 지도에 칠하기: 칸들을 캔버스 그림 한 장으로 만들어 지도 위에 얹음 (칸 수천 개도 가볍게)
  function render(t) {
    const cells = cellsUpto(t);
    const key = mode + "|" + cells.length + "|" + shown;
    if (key === renderedKey) return;
    renderedKey = key;
    if (overlay) { App.map.removeLayer(overlay); overlay = null; }
    if (!shown || !cells.length || !origin) return;
    let a0 = Infinity, a1 = -Infinity, b0 = Infinity, b1 = -Infinity;
    const ij = cells.map((k) => k.split(",").map(Number));
    for (const [i, j] of ij) { if (i < a0) a0 = i; if (i > a1) a1 = i; if (j < b0) b0 = j; if (j > b1) b1 = j; }
    const W = a1 - a0 + 1, H = b1 - b0 + 1;
    const sc = Math.max(1, Math.floor(1600 / Math.max(W, H)));
    const cv = document.createElement("canvas");
    cv.width = Math.min(4096, W * sc); cv.height = Math.min(4096, H * sc);
    const g = cv.getContext("2d");
    g.fillStyle = "#22d3ee";
    for (const [i, j] of ij) g.fillRect((i - a0) * sc, (b1 - j) * sc, sc, sc);
    const ll = (x, y) => [origin[0] + y / 111320, origin[1] + x / mE];
    const bounds = [ll(a0 * CELL, b0 * CELL), ll((a1 + 1) * CELL, (b1 + 1) * CELL)];
    overlay = L.imageOverlay(cv.toDataURL("image/png"), bounds, { opacity: 0.22, interactive: false, className: "coverage-img" }).addTo(App.map);
    overlay.bringToBack();
  }

  function stats(t) {
    const n = mode ? cellsUpto(t).length : 0;
    const first = pathPts.length ? pathPts[0][0] : t;
    return { area_m2: n * CELL * CELL, path_m: Math.round(pathLen(t)), dur_s: Math.max(0, Math.round(Math.min(t, pathPts.length ? pathPts[pathPts.length - 1][0] : t) - first)) };
  }
  App.coverageStats = () => stats(nowT());

  // 보고서용: 5 m 칸으로 묶은 촬영 범위 + 드론 경로 + 객체 위치 (지역 좌표 m)
  App.coverageReport = function (tCut) {
    if (!origin || !grid.size) return null;
    const cutR = mode === "replay" && tCut != null ? tCut : null;
    const t = nowT(), cells = new Set();
    for (const [k, t0] of grid) {
      if (t0 > t && mode === "live") continue;
      if (cutR != null && t0 > cutR) continue;
      const [i, j] = k.split(",").map(Number);
      cells.add(Math.floor((i * CELL) / 5) + "," + Math.floor((j * CELL) / 5));
    }
    if (cells.size > 20000) return null;
    const tEnd = mode === "replay" ? (cutR != null ? cutR : Infinity) : t;
    const path = pathPts.filter((p) => p[0] <= tEnd);
    const step = Math.max(1, Math.ceil(path.length / 400));
    const objs = (mode === "replay" && App.replay && App.replay.reportData ? App.replay.reportData(cutR).objects : App.objects || []);
    const st = mode === "replay" ? stats(cutR != null ? cutR : Infinity) : stats(t);
    return {
      cell: 5, area_m2: st.area_m2, path_m: st.path_m, dur_s: st.dur_s,
      cells: [...cells].map((k) => k.split(",").map(Number)),
      path: path.filter((_, i) => i % step === 0).map((p) => toXY([p[1], p[2]]).map((v) => Math.round(v * 10) / 10)),
      objects: objs.filter((o) => o.lat != null).map((o) => [o.cls === "person" ? "p" : "v"].concat(toXY([o.lat, o.lon]).map((v) => Math.round(v * 10) / 10))),
    };
  };

  // ---- 리플레이: 영상 전체를 미리 계산
  function loadReplay() {
    const R = App.replay;
    const fps = R.footprints(), path = R.path();
    reset(path.length ? [path[0][1], path[0][2]] : fps.length ? fps[0][1][0] : null);
    pathPts = path;
    for (const [t, c] of fps) addPoly(c, t);
    mode = "replay";
  }

  // ---- 지도 버튼
  const btn = document.createElement("button");
  btn.type = "button"; btn.id = "coverageToggle"; btn.className = "ghost coverage-toggle" + (shown ? " on" : "");
  btn.title = "카메라가 지금까지 촬영한 땅을 지도에 칠해서 표시";
  btn.innerHTML = "▦ 정찰 범위";
  const zt = $("zoneToggle");
  if (zt) zt.after(btn);
  btn.addEventListener("click", () => {
    shown = !shown; btn.classList.toggle("on", shown);
    try { localStorage.setItem("dash.coverage", shown ? "1" : "0"); } catch (e) {}
    render(nowT());
  });

  let curKey = null;
  App.on((s) => {
    const key = App.replay ? "r:" + App.replay.item.id : "live";
    if (key !== curKey) {
      curKey = key;
      if (App.replay && App.replay.footprints) loadReplay(); else { reset(null); mode = "live"; }
    }
    if (mode === "live") {
      const now = Date.now() / 1000;
      if (s.lat != null && s.altitude != null && s.altitude > 2) {
        const last = pathPts[pathPts.length - 1];
        if (!last || now - last[0] >= 1) {
          if (!origin) reset([s.lat, s.lon]);
          pathPts.push([now, s.lat, s.lon]);
          if (pathPts.length > 7200) pathPts.shift();
        }
      }
      if (s.footprint && now - lastLiveFp >= 1) { lastLiveFp = now; addPoly(s.footprint, now); }
    }
    const t = Date.now();
    if (t - lastRender > 1000) { lastRender = t; render(nowT()); }
  });
})();
