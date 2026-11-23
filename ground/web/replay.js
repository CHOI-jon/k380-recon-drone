// 녹화 영상 리플레이 — 영상 재생 시각에 맞춰 track.json(비행 데이터 + 탐지·위치추정 결과)을
// 지도·비행 상태·객체 목록·스냅샷에 표시한다. 자료 보관함(lib.js)에서 항목을 고르면 바뀐다.
//   App.replayCtl.load(item)  : item = {id, name}  (영상 /lib/<id>/video, 데이터 /lib/<id>/track.json)
//   App.replayCtl.stop()      : 실시간 화면(또는 시뮬레이션)으로 돌아감
(function () {
  if (typeof App === "undefined" || !App.ready) return;
  const LAST_KEY = "dash.lastReplay";
  let cur = null, layers = null, video = null, canvas = null, raf = 0, tag = null;

  const store = {
    get() { try { return JSON.parse(localStorage.getItem(LAST_KEY) || "null"); } catch (e) { return null; } },
    set(v) { try { localStorage.setItem(LAST_KEY, JSON.stringify(v)); } catch (e) {} },
  };

  function urls(item) {
    return { video: `/lib/${encodeURIComponent(item.id)}/video?v=${item.v || ""}`,
             track: `/lib/${encodeURIComponent(item.id)}/track.json?v=${Date.now()}` };
  }

  function showVideo(src) {
    const frame = document.querySelector(".video-frame");
    const img = document.getElementById("droneVideo");
    const empty = document.getElementById("videoEmpty");
    if (!video) {
      video = document.createElement("video");
      video.className = "demo-video";
      video.muted = true; video.loop = true; video.autoplay = true; video.playsInline = true; video.controls = true;
      video.addEventListener("error", () => {
        const e = video.error;
        empty.hidden = false;
        empty.innerHTML = `이 영상을 브라우저가 재생하지 못합니다${e && e.code === 4 ? " (형식 미지원 — H.265 영상은 Chrome 최신 버전·하드웨어 가속 필요)" : ""}.<br>지도·객체 목록은 계속 볼 수 있습니다.`;
      });
      video.addEventListener("loadeddata", () => { empty.hidden = true; video.play().catch(() => {}); });
      frame.appendChild(video);
      canvas = document.createElement("canvas");
      canvas.className = "replay-overlay";
      frame.appendChild(canvas);
    }
    if (img) img.style.display = "none";
    video.src = src;
    video.load();
  }

  function hideVideo() {
    cancelAnimationFrame(raf);
    if (video) { video.pause(); video.removeAttribute("src"); video.load(); video.remove(); video = null; }
    if (canvas) { canvas.remove(); canvas = null; }
    const img = document.getElementById("droneVideo");
    const empty = document.getElementById("videoEmpty");
    if (img) img.style.display = "";
    if (empty) {
      const live = App.cfg && App.cfg.video_url;
      empty.hidden = live && img && img.naturalWidth > 0;
      if (!live) empty.textContent = "시뮬레이션 모드 — 영상 없음 (자료 보관함에서 자료를 고르세요)";
    }
  }

  function teardown() {
    if (layers) { layers.forEach((l) => App.map.removeLayer(l)); layers = null; }
    if (tag) { tag.remove(); tag = null; }
    App.replay = null;
    if (typeof dronePath !== "undefined") dronePath.setLatLngs([]);
  }

  function setTag(text) {
    const h2 = document.querySelector(".map-card .card-header h2");
    if (!h2) return;
    tag = document.createElement("span");
    tag.className = "replay-tag";
    tag.textContent = text;
    h2.appendChild(tag);
  }

  function start(D, item) {
    teardown();
    const T = D.track, ti = T.map((r) => r[0]);
    let off = 0;
    const yawU = T.map((r, i) => {
      if (i) { const d = r[4] - T[i - 1][4]; if (d > 180) off -= 360; else if (d < -180) off += 360; }
      return r[4] + off;
    });
    const idMap = {};
    const dets = D.dets.map((d) => {
      const c = d.slice();
      if (c[6]) { const n = c[6][0] + "-" + parseInt(c[6].slice(2), 10); idMap[n] = c[6]; c[6] = n; }
      return c;
    }).sort((a, b) => a[0] - b[0]);
    const dt = 1 / (D.det_fps || 4);
    const base = new Date(D.start).getTime();
    const p2 = (n) => String(n).padStart(2, "0");
    const clock = (t) => { const d = new Date(base + t * 1000);
      return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`; };
    const mE = 111320 * Math.cos(((T[0] ? T[0][1] : 37.87) * Math.PI) / 180);
    const getT = () => (video && video.readyState > 0 ? video.currentTime : null);
    // 감시구역 판정 (리플레이용 — 실시간은 지상국이 판정)
    const inPoly = (la, lo, pts) => {
      let c = false;
      for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
        const [yi, xi] = pts[i], [yj, xj] = pts[j];
        if ((yi > la) !== (yj > la) && lo < ((xj - xi) * (la - yi)) / (yj - yi) + xi) c = !c;
      }
      return c;
    };

    function at(t) {
      let lo = 0, hi = ti.length - 1;
      if (t <= ti[0]) hi = 0; else if (t >= ti[hi]) lo = hi;
      else { while (hi - lo > 1) { const m = (lo + hi) >> 1; if (ti[m] <= t) lo = m; else hi = m; } }
      const a = T[lo], b = T[hi], f = hi === lo ? 0 : (t - a[0]) / (b[0] - a[0]);
      const L_ = (i) => a[i] + (b[i] - a[i]) * f;
      return { lat: L_(1), lon: L_(2), alt: L_(3), yaw: (((yawU[lo] + (yawU[hi] - yawU[lo]) * f) % 360) + 360) % 360,
               gp: L_(5), roll: a[6], pitch: a[7], speed: a[8], mode: a[9] || "", idx: lo };
    }

    function ground(st, u, v) {
      const rad = Math.PI / 180, a = -st.gp * rad, psi = st.yaw * rad;
      const h = [Math.cos(psi), Math.sin(psi), 0], r = [-Math.sin(psi), Math.cos(psi), 0];
      const f = [Math.cos(a) * h[0], Math.cos(a) * h[1], Math.sin(a)];
      const ud = [-Math.sin(a) * h[0], -Math.sin(a) * h[1], Math.cos(a)];
      const xn = (u - 0.5) * 2 * Math.tan((D.hfov * rad) / 2), yn = (v - 0.5) * 2 * Math.tan((D.vfov * rad) / 2);
      const R = [0, 1, 2].map((i) => f[i] + xn * r[i] + yn * ud[i]);
      if (R[2] <= 1e-6) return null;
      const k = st.alt / R[2];
      return [st.lat + (k * R[0]) / 111320, st.lon + (k * R[1]) / mE];
    }
    const foot = L.polygon([], { color: "#facc15", weight: 1, dashArray: "4 4", fillOpacity: 0.06, interactive: false }).addTo(App.map);
    const footTop = L.polyline([], { color: "#facc15", weight: 5, opacity: 0.95, interactive: false }).addTo(App.map);
    const footLabel = L.marker([0, 0], { interactive: false, keyboard: false,
      icon: L.divIcon({ className: "foot-top-label", html: "▲ 영상 위쪽", iconSize: null }) });
    layers = [foot, footTop, footLabel];

    // 이탈 판정용: 0.5초 간격 카메라 촬영 범위(가장자리 8% 제외) 미리 계산
    const FP = [];
    if (T.length) {
      for (let k = 0; k <= T[T.length - 1][0]; k += 0.5) {
        const st = at(k);
        const c = st.alt > 2 ? [[0.08, 0.08], [0.92, 0.08], [0.92, 0.92], [0.08, 0.92]].map(([u, v]) => ground(st, u, v)) : [];
        FP.push([k, c.length === 4 && c.every(Boolean) ? c : null]);
      }
    }
    // 계속 화면 안에 있는데 탐지가 없으면 '이탈': 사람 3초, 차량 5초 (주차 차량을 잠깐 못 잡아도 바로 지우지 않게)
    const LEFT_RUN = { P: 6, V: 10 };
    const leftCache = {};
    function firstLeft(id, lastT, la, lo) {
      const need = LEFT_RUN[id[0]] || 6;
      const key = id + "|" + lastT;
      if (key in leftCache) return leftCache[key];
      let run = 0, res = Infinity;
      for (const [k, poly] of FP) {
        if (k <= lastT + 0.5) continue;
        if (poly && inPoly(la, lo, poly)) { if (++run >= need) { res = k; break; } } else run = 0;
      }
      return (leftCache[key] = res);
    }

    const label = item.id === "demo" ? "리플레이 · 기본 데모 (매빅 0314 · VisDrone 640)"
      : `리플레이 · ${item.name}${D.model ? " · " + D.model : ""}`;
    setTag(label);
    if (T.length) App.map.setView([T[0][1], T[0][2]], 18);

    // 영상 위 탐지 박스 (재분석한 항목만 — 기본 데모 영상은 박스가 이미 그려져 있음)
    function drawBoxes() {
      raf = requestAnimationFrame(drawBoxes);
      if (!canvas || !video || !D.overlay) return;
      const fw = canvas.parentElement.clientWidth, fh = canvas.parentElement.clientHeight;
      if (canvas.width !== fw || canvas.height !== fh) { canvas.width = fw; canvas.height = fh; }
      const g = canvas.getContext("2d");
      g.clearRect(0, 0, fw, fh);
      const vw = video.videoWidth, vh = video.videoHeight;
      if (!vw || !vh) return;
      const s = Math.min(fw / vw, fh / vh), w = vw * s, h = vh * s, x0 = (fw - w) / 2, y0 = (fh - h) / 2;
      const t = video.currentTime;
      g.lineWidth = 2; g.font = "bold 12px 'Malgun Gothic', sans-serif";
      for (const d of dets) {
        if (d[0] > t + 0.01) break;
        if (d[0] <= t - dt - 0.01 || !d[7]) continue;
        const [a, b, c, e] = d[7];
        const col = d[1] === "p" ? "#f43f5e" : "#38bdf8";
        g.strokeStyle = col;
        g.strokeRect(x0 + a * w, y0 + b * h, (c - a) * w, (e - b) * h);
        const txt = `${d[1] === "p" ? "사람" : "차량"} ${d[2].toFixed(2)}${d[6] ? " " + d[6] : ""}`;
        const tw = g.measureText(txt).width + 6;
        g.fillStyle = col;
        g.fillRect(x0 + a * w - 1, y0 + b * h - 16, tw, 16);
        g.fillStyle = d[1] === "p" ? "#fff" : "#082f49";
        g.fillText(txt, x0 + a * w + 2, y0 + b * h - 4);
      }
    }
    cancelAnimationFrame(raf);
    if (D.overlay) drawBoxes(); else if (canvas) canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);

    // 시각 t까지의 탐지를 객체·경보로 모음 (지도는 건드리지 않음 — 화면 갱신과 보고서가 같이 씀)
    function collect(t) {
    const agg = {};
    let nowP = 0, nowV = 0;
    for (const d of dets) {
      if (d[0] > t) break;
      if (d[0] > t - dt) { if (d[1] === "p") nowP++; else nowV++; }
      const id = d[6];
      if (!id || d[3] == null) continue;
      const o = agg[id] || (agg[id] = { id, cls: d[1] === "p" ? "person" : "vehicle", conf: 0, la: 0, lo: 0, err: 0, n: 0, first: d[0], last: d[0], rec: [] });
      o.conf = Math.max(o.conf, d[2]); o.la += d[3]; o.lo += d[4]; o.err += d[5] || 0; o.n++; o.last = d[0];
      o.rec.push([d[3], d[4]]); if (o.rec.length > 3) o.rec.shift();
      if (o.n === 2) o.confirm = d[0];
    }
    const objMeta = D.objs || {};
    const zones = App.zones || [];
    const alerts = [];
    const objects = Object.values(agg).filter((o) => o.n >= 2).map((o) => {
      // 지도 위치 = 최근 3회 탐지 평균 (이동한 사람이 옛 자리에 남지 않게)
      const la = o.rec.reduce((a, r) => a + r[0], 0) / o.rec.length, lo = o.rec.reduce((a, r) => a + r[1], 0) / o.rec.length;
      o.pla = la; o.plo = lo;
      o.left = t >= firstLeft(o.id, o.last, la, lo);
      const zin = zones.filter((z) => z.points && z.points.length >= 3 && inPoly(la, lo, z.points));
      zin.forEach((z, k) => alerts.push({ seq: Math.round(o.confirm * 100) * 10 + k + 1, t: o.confirm, time: clock(o.confirm).slice(11),
        zone: z.name, zone_id: z.id, obj_id: o.id, cls: o.cls, lat: la, lon: lo }));
      return Object.assign(o, { zin: zin.map((z) => z.id) });
    }).map((o) => ({
      id: o.id, cls: o.cls, conf: Math.round(o.conf * 100) / 100,
      lat: o.pla, lon: o.plo, err: Math.round((o.err / o.n) * 10) / 10, left: o.left,
      hits: o.n, first_seen: clock(o.first), last_seen: clock(o.last),
      active: t - o.last < 3, zones: o.left ? [] : o.zin, snap: (objMeta[idMap[o.id]] || {}).snap || null,
      confirm_t: o.confirm, left_t: o.left ? firstLeft(o.id, o.last, o.pla, o.plo) : null,
    }));
    alerts.sort((a, b) => a.seq - b.seq);
      return { objects, alerts, nowP, nowV };
    }

    let lastIdx = -1, lastPerf = null;
    const PF = D.perf || null;
    function feedPerf(t) {
      if (!PF || !App.perfCtl) return;
      const key = Math.floor(t);
      if (key === lastPerf) return;
      lastPerf = key;
      const h = [];
      for (const r of PF) {
        if (r[0] > t) break;
        if (r[0] < t - 180) continue;
        h.push({ t: base / 1000 + r[0], fps: r[1], infer_ms: r[2], temp_c: r[3], cpu_pct: r[4], throttled: r[5],
                 e2e_ms: null, rtt_ms: null, fc_rate: null, pi_rate: null, exp_us: r[6], gain: r[7] });
      }
      App.perfCtl.set(h);
    }
    App.replay = {
      data: D, item,
      trail(id) {
        const t = getT() ?? Infinity;
        return dets.filter((d) => d[6] === id && d[0] <= t && d[3] != null).map((d) => ({ lat: d[3], lon: d[4], time: clock(d[0]) }));
      },
      // 정찰 범위용: 1초 간격 카메라 촬영 범위(영상 전체 네 모서리) + 드론 경로
      footprints() {
        const out = [];
        if (!T.length) return out;
        for (let k = 0; k <= T[T.length - 1][0]; k += 1) {
          const st = at(k);
          if (st.alt <= 2) continue;
          const c = [[0, 0], [1, 0], [1, 1], [0, 1]].map(([u, v]) => ground(st, u, v));
          if (c.every(Boolean) && c.every((p) => Math.hypot((p[0] - st.lat) * 111320, (p[1] - st.lon) * mE) < 300)) out.push([k, c]);
        }
        return out;
      },
      path() { return T.map((r) => [r[0], r[1], r[2]]); },
      // 정찰 상황 일지: 영상 전체 사건 목록 (화면에는 재생 시각까지만 표시)
      events(tCut) {
        const tFull = video && video.duration ? video.duration : (T.length ? T[T.length - 1][0] : 0);
        const tEnd = tCut != null && tCut < tFull ? Math.max(0, tCut) : tFull;
        const ev = [], push = (t, type, text, extra) => ev.push(Object.assign({ t, time: clock(t).slice(11), type, text }, extra || {}));
        if (T.length) {
          let air = T[0][3] > 3, mode = T[0][9] || "";
          push(T[0][0], "flight", air ? `기록 시작 — 비행 중 (고도 ${Math.round(T[0][3])} m)` : "기록 시작 — 지상 대기");
          let hiAlt = 0, hiT = 0;
          for (const r of T) {
            if (r[0] > tEnd) break;
            if (!air && r[3] > 3) { air = true; push(r[0], "flight", "이륙"); }
            else if (air && r[3] < 1.0) { air = false; push(r[0], "flight", "착륙"); }
            if (r[9] && r[9] !== mode) { push(r[0], "flight", `비행 모드 → ${r[9]}`); mode = r[9]; }
            if (r[3] > hiAlt) { hiAlt = r[3]; hiT = r[0]; }
          }
          if (hiAlt > 3) push(hiT, "flight", `최고 고도 ${Math.round(hiAlt)} m`);
        }
        const c = collect(tEnd);
        const KO = { person: "사람", vehicle: "차량" };
        for (const o of c.objects) {
          push(o.confirm_t, "obj", `${KO[o.cls]} ${o.id} 식별`, { id: o.id, cls: o.cls, snap: o.snap });
          if (o.left_t != null && o.left_t <= tEnd) push(o.left_t, "left", `${KO[o.cls]} ${o.id} 이탈 확인 (시야 안에서 사라짐)`, { id: o.id, cls: o.cls });
        }
        for (const a of c.alerts) push(a.t, "alert", `${a.zone} 침입 — ${KO[a.cls]} ${a.obj_id}`, { id: a.obj_id, cls: a.cls });
        // 한 화면 동시 탐지 최대 갱신 (2 이상부터)
        const fr = {};
        for (const d of dets) { const k = Math.round(d[0] * 100) / 100; (fr[k] || (fr[k] = { t: d[0], p: 0, v: 0 }))[d[1] === "p" ? "p" : "v"]++; }
        let mp = 1, mv = 1;
        for (const f of Object.values(fr).sort((a, b) => a.t - b.t)) {
          if (f.p > mp) { mp = f.p; push(f.t, "peak", `동시 탐지 최대 갱신 — 사람 ${f.p}명`, { cls: "person" }); }
          if (f.v > mv) { mv = f.v; push(f.t, "peak", `동시 탐지 최대 갱신 — 차량 ${f.v}대`, { cls: "vehicle" }); }
        }
        if (T.length) push(tEnd, "flight", tEnd < tFull - 0.5 ? "보고서 기준 시점 (여기까지 분석)" : "기록 종료");
        const order = { flight: 0, alert: 1, obj: 2, left: 3, peak: 4 };
        return ev.filter((e) => e.t != null && isFinite(e.t) && e.t <= tEnd + 0.01).sort((a, b) => a.t - b.t || order[a.type] - order[b.type]);
      },
      // 객체 수 그래프용: 프레임별 탐지 수 + 객체가 식별(2회 탐지)된 시각
      series() {
        const dur = video && video.duration ? video.duration : (T.length ? T[T.length - 1][0] : 0);
        const fr = new Map(), cnt = {}, confirms = [];
        for (const d of dets) {
          const k = Math.round(d[0] * 100) / 100;
          let f = fr.get(k); if (!f) fr.set(k, (f = [0, 0]));
          f[d[1] === "p" ? 0 : 1]++;
          const id = d[6];
          if (!id || d[3] == null) continue;
          cnt[id] = (cnt[id] || 0) + 1;
          if (cnt[id] === 2) confirms.push([d[0], d[1] === "p" ? "person" : "vehicle"]);
        }
        return { dur, frames: [...fr].map(([t, c]) => [t, c[0], c[1]]), confirms, clock: (t) => clock(t).slice(11) };
      },
      // 보고서용: 영상 전체(끝 시점) 기준 결과 + 영상·비행 정보
      fullDuration() { return video && video.duration ? video.duration : (T.length ? T[T.length - 1][0] : 0); },
      // tCut: 보고서 기준 시점(영상 초). 없으면 영상 끝까지
      reportData(tCut) {
        const tFull = this.fullDuration();
        const tEnd = tCut != null && tCut < tFull ? Math.max(0, tCut) : tFull;
        const c = collect(tEnd);
        const st = T.length ? at(tEnd) : {};
        const Tc = T.filter((r) => r[0] <= tEnd);
        let alts = Tc.filter((r) => r[3] > 2).map((r) => r[3]), dist = 0;
        for (let i = 1; i < Tc.length; i++) dist += Math.hypot((Tc[i][1] - Tc[i - 1][1]) * 111320, (Tc[i][2] - Tc[i - 1][2]) * mE);
        return {
          id: item.id, name: item.id === "demo" ? "기본 데모 (매빅 0314)" : item.name, kind: D.kind || "mavic",
          model: D.model || (item.id === "demo" ? "VisDrone 640 + 추가학습 v1" : ""), source: D.source || "",
          start: D.start, duration: Math.round(tEnd), clock_end: clock(tEnd),
          cut: tEnd < tFull - 0.5 ? { t: Math.round(tEnd * 10) / 10, full: Math.round(tFull) } : null,
          alt_min: alts.length ? Math.round(Math.min(...alts) * 10) / 10 : null, alt_max: alts.length ? Math.round(Math.max(...alts) * 10) / 10 : null,
          path_m: Math.round(dist),
          drone: { lat: st.lat, lon: st.lon, alt_m: st.alt != null ? Math.round(st.alt * 10) / 10 : null, mode: st.mode || "" },
          objects: c.objects, alerts: c.alerts,
        };
      },
      patch(s) {
        const t = getT();
        if (t == null || !T.length) return s;
        const st = at(t);
        if (st.idx !== lastIdx) {
          const pts = [];
          for (let i = 0; i <= st.idx; i += 2) pts.push([T[i][1], T[i][2]]);
          dronePath.setLatLngs(pts);
          lastIdx = st.idx;
        }
        if (st.alt > 2) {
          const c = [[0, 0], [1, 0], [1, 1], [0, 1]].map(([u, v]) => ground(st, u, v)).filter(Boolean);
          foot.setLatLngs(c.length === 4 ? c : []);
          if (c.length === 4) {
            footTop.setLatLngs([c[0], c[1]]);
            footLabel.setLatLng([(c[0][0] + c[1][0]) / 2, (c[0][1] + c[1][1]) / 2]);
            if (!App.map.hasLayer(footLabel)) footLabel.addTo(App.map);
          } else { footTop.setLatLngs([]); footLabel.remove(); }
        } else { foot.setLatLngs([]); footTop.setLatLngs([]); footLabel.remove(); }

        feedPerf(t);
        const { objects, alerts, nowP, nowV } = collect(t);
        const paused = video && video.paused;
        return Object.assign({}, s, {
          altitude: st.alt, speed: st.speed, heading: st.yaw, lat: st.lat, lon: st.lon,
          roll: st.roll, pitch: st.pitch, gps: D.kind === "k380" ? "K380 기록" : "DJI GNSS", battery: null, voltage: null,
          mode: (paused ? "REPLAY 일시정지" : "REPLAY") + (st.mode ? " · " + st.mode : ""), armed: false,
          fps: D.overlay ? `${D.det_fps || 4} (재분석)` : "결과 영상",
          bench: false, link: Object.assign({}, s.link, { fc: true, pi: true }),
          detections: { person: nowP, vehicle: nowV, other: 0 }, objects, alerts,
        });
      },
    };
  }

  async function load(item) {
    const u = urls(item);
    const trackUrl = item.id === "demo" ? `demo_track.json?v=${Date.now()}` : u.track;
    const videoUrl = item.id === "demo" ? "demo.mp4?v=ft1" : u.video;
    const r = await fetch(trackUrl);
    if (!r.ok) throw new Error("분석 결과(track.json)가 없습니다 — 먼저 분석하세요");
    const D = await r.json();
    showVideo(videoUrl);
    start(D, item);
    cur = item;
    store.set({ id: item.id, name: item.name });
    document.dispatchEvent(new CustomEvent("replay-change", { detail: item }));
  }

  function stop() {
    teardown();
    if (App.perfCtl) App.perfCtl.live();
    hideVideo();
    cur = null;
    store.set(null);
    document.dispatchEvent(new CustomEvent("replay-change", { detail: null }));
  }

  App.replayCtl = { load, stop, current: () => cur };

  // 시작: 실시간 파이 영상이 없으면 마지막으로 본 자료(없으면 기본 데모)를 자동 재생
  App.ready.then((cfg) => {
    if (cfg && cfg.video_url) return;
    const last = store.get();
    const tryDemo = () => load({ id: "demo", name: "기본 데모" }).catch(() => {});
    if (last && last.id && last.id !== "demo") load(last).catch(tryDemo);
    else tryDemo();
  });
})();
