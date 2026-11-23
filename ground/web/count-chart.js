// 객체 수 변화 그래프 — 화면 속 탐지 수(동시에 보인 수) / 누적 식별 객체 수
// 리플레이: 영상 전체 구간을 미리 계산, 재생 위치 표시, 클릭하면 그 시각으로 이동
// 실시간: 접속 후 1초 단위로 기록 (최대 60분)
// App.countSeries() → 보고서 그래프용 구간 데이터
(function () {
  if (typeof App === "undefined" || !App.ready) return;
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, a = {}) => { const e = document.createElementNS(NS, tag); for (const k in a) e.setAttribute(k, a[k]); return e; };
  const COL = { p: "#f43f5e", v: "#38bdf8" };
  const MAXB = 300;
  let mode = "now", cacheId = null, cache = null, head = null, lastDraw = 0, geom = null;
  const live = [];          // [{sec(epoch), label, p, v, cp, cv}]
  let liveKey = null;

  const p2 = (n) => String(n).padStart(2, "0");
  const hms = (d) => `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
  const video = () => document.querySelector("video.demo-video");

  // ---- 리플레이 구간 데이터
  function replaySeries() {
    const R = App.replay;
    if (!R || !R.series) return null;
    const key = R.item.id + "|" + (video() && video().duration ? Math.round(video().duration) : 0);
    if (cacheId === key) return cache;
    const S = R.series();
    if (!S.dur) return null;
    const bin = Math.max(1, Math.ceil(S.dur / MAXB)), n = Math.floor(S.dur / bin) + 1;
    const p = new Array(n).fill(0), v = new Array(n).fill(0), cp = new Array(n).fill(0), cv = new Array(n).fill(0);
    for (const [t, a, b] of S.frames) {
      const i = Math.min(n - 1, Math.floor(t / bin));
      if (a > p[i]) p[i] = a;
      if (b > v[i]) v[i] = b;
    }
    let k = 0, np = 0, nv = 0;
    for (let i = 0; i < n; i++) {
      const end = (i + 1) * bin;
      while (k < S.confirms.length && S.confirms[k][0] < end) { if (S.confirms[k][1] === "person") np++; else nv++; k++; }
      cp[i] = np; cv[i] = nv;
    }
    const t = [...Array(n).keys()].map((i) => i * bin);
    cache = { kind: "replay", bin, dur: S.dur, t, labels: t.map((x) => S.clock(x)), p, v, cp, cv };
    cacheId = key;
    return cache;
  }

  // ---- 실시간 구간 데이터 (1초 단위 기록 → 최대 300구간으로 묶음)
  function liveSeries() {
    if (!live.length) return null;
    const bin = Math.max(1, Math.ceil(live.length / MAXB));
    const out = { kind: "live", bin, t: [], labels: [], p: [], v: [], cp: [], cv: [] };
    for (let i = 0; i < live.length; i += bin) {
      const g = live.slice(i, i + bin);
      out.t.push(g[0].sec - live[0].sec);
      out.labels.push(g[0].label);
      out.p.push(Math.max(...g.map((x) => x.p)));
      out.v.push(Math.max(...g.map((x) => x.v)));
      out.cp.push(g[g.length - 1].cp);
      out.cv.push(g[g.length - 1].cv);
    }
    out.dur = live[live.length - 1].sec - live[0].sec;
    return out;
  }

  const current = () => (App.replay ? replaySeries() : liveSeries());

  // tCut: 리플레이 보고서 기준 시점(영상 초) — 그 시점까지만
  App.countSeries = function (tCut) {
    const s = current();
    if (!s) return null;
    let n = s.t.length;
    if (tCut != null && s.kind === "replay") { n = s.t.filter((x) => x <= tCut).length; if (!n) return null; }
    const cut = (a) => a.slice(0, n);
    return { kind: s.kind, bin: s.bin, labels: cut(s.labels), t: cut(s.t), p: cut(s.p), v: cut(s.v), cp: cut(s.cp), cv: cut(s.cv) };
  };

  // ---- 그리기
  function draw(s) {
    const box = $("ccChart");
    box.classList.toggle("seekable", !!App.replay);
    if (!s || !s.t.length) { box.innerHTML = `<p class="muted">${App.replay ? "영상 정보를 읽는 중…" : "탐지 데이터가 들어오면 그래프가 그려집니다."}</p>`; head = null; geom = null; return; }
    const W = Math.max(320, box.clientWidth), H = box.clientHeight || 190;
    const P = { l: 34, r: 12, t: 10, b: 22 };
    const A = mode === "now" ? s.p : s.cp, B = mode === "now" ? s.v : s.cv;
    const tMax = Math.max(s.kind === "replay" ? s.dur : s.t[s.t.length - 1], 1);
    const yMax = Math.max(2, ...A, ...B);
    const step = yMax <= 5 ? 1 : yMax <= 12 ? 2 : yMax <= 30 ? 5 : yMax <= 60 ? 10 : 20;
    const top = Math.ceil(yMax / step) * step;
    const X = (t) => P.l + (t / tMax) * (W - P.l - P.r), Y = (v) => H - P.b - (v / top) * (H - P.t - P.b);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}` });
    for (let y = 0; y <= top; y += step) {
      svg.appendChild(el("line", { x1: P.l, x2: W - P.r, y1: Y(y), y2: Y(y), stroke: "#1e293b", "stroke-width": 1 }));
      const tx = el("text", { x: P.l - 6, y: Y(y) + 4, "text-anchor": "end", fill: "#64748b", "font-size": 11 }); tx.textContent = y; svg.appendChild(tx);
    }
    const ticks = 6;
    for (let k = 0; k <= ticks; k++) {
      const i = Math.round((k / ticks) * (s.t.length - 1));
      const tx = el("text", { x: X(s.t[i]), y: H - 6, "text-anchor": k === 0 ? "start" : k === ticks ? "end" : "middle", fill: "#64748b", "font-size": 11 });
      tx.textContent = s.labels[i]; svg.appendChild(tx);
    }
    const path = (arr, stepwise) => arr.map((v, i) => {
      const x = X(s.t[i]), y = Y(v);
      if (i === 0) return `M${x},${y}`;
      return stepwise ? `H${x}V${y}` : `L${x},${y}`;
    }).join("");
    const stepwise = mode === "cum";
    for (const [arr, c] of [[B, COL.v], [A, COL.p]]) {
      const d = path(arr, stepwise);
      if (mode === "now") svg.appendChild(el("path", { d: `${d}L${X(s.t[s.t.length - 1])},${Y(0)}L${X(s.t[0])},${Y(0)}Z`, fill: c, "fill-opacity": 0.12 }));
      svg.appendChild(el("path", { d, fill: "none", stroke: c, "stroke-width": 1.8, "stroke-linejoin": "round" }));
    }
    head = el("line", { y1: P.t, y2: H - P.b, stroke: "#facc15", "stroke-width": 1.5, visibility: "hidden" });
    svg.appendChild(head);
    const hover = el("line", { y1: P.t, y2: H - P.b, stroke: "#94a3b8", "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" });
    svg.appendChild(hover);
    const hit = el("rect", { x: P.l, y: P.t, width: W - P.l - P.r, height: H - P.t - P.b, fill: "transparent" });
    svg.appendChild(hit);
    const idxAt = (evt) => {
      const r = svg.getBoundingClientRect();
      const t = ((evt.clientX - r.left) * (W / r.width) - P.l) / (W - P.l - P.r) * tMax;
      let lo = 0; while (lo < s.t.length - 1 && s.t[lo + 1] <= t) lo++;
      return { i: lo, t: Math.max(0, Math.min(tMax, t)) };
    };
    hit.addEventListener("mousemove", (evt) => {
      const { i } = idxAt(evt);
      hover.setAttribute("x1", X(s.t[i])); hover.setAttribute("x2", X(s.t[i])); hover.setAttribute("visibility", "visible");
      Charts.showTip(evt, `<b>${s.labels[i]}</b><br>${mode === "now" ? "화면 속 탐지" : "누적 식별"} — 사람 ${A[i]} · 차량 ${B[i]}` +
        (App.replay ? `<br><span style="opacity:.7">클릭하면 이 시각으로 이동</span>` : ""));
    });
    hit.addEventListener("mouseleave", () => { hover.setAttribute("visibility", "hidden"); Charts.hideTip(); });
    hit.addEventListener("click", (evt) => {
      const v = video();
      if (App.replay && v) { v.currentTime = idxAt(evt).t; }
    });
    box.innerHTML = ""; box.appendChild(svg);
    geom = { X, tMax };

    // 요약
    const mi = (arr) => arr.reduce((b, v, i) => (v > arr[b] ? i : b), 0);
    const ip = mi(s.p), iv = mi(s.v);
    $("ccSum").innerHTML = `최대 동시 탐지 사람 <b>${s.p[ip]}</b> (${s.labels[ip]}) · 차량 <b>${s.v[iv]}</b> (${s.labels[iv]})` +
      ` &nbsp;|&nbsp; 누적 식별 사람 <b>${s.cp[s.cp.length - 1]}</b> · 차량 <b>${s.cv[s.cv.length - 1]}</b>`;
    $("ccNote").textContent = (mode === "now"
      ? "화면 속 탐지 수: 그 순간 영상 한 화면에 동시에 잡힌 수 (구간별 최대값)"
      : "누적 식별 객체: 2회 이상 탐지되어 객체로 확정된 수의 누적 — 탐지 객체 목록의 개수와 같음")
      + (App.replay ? " · 노란 선 = 현재 재생 위치" : ` · 실시간 기록 ${Math.round(s.dur / 60)}분`);
  }

  function moveHead() {
    const v = video();
    if (!head || !geom || !App.replay || !v) { if (head) head.setAttribute("visibility", "hidden"); return; }
    const x = geom.X(Math.min(v.currentTime, geom.tMax));
    head.setAttribute("x1", x); head.setAttribute("x2", x); head.setAttribute("visibility", "visible");
  }

  document.querySelectorAll(".cc-seg button").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll(".cc-seg button").forEach((x) => x.classList.toggle("on", x === b));
    draw(current()); moveHead();
  }));
  let rz = 0;
  window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { draw(current()); moveHead(); }, 150); });

  let drawnKey = null;
  App.on((s) => {
    if (!App.replay) {
      // 실시간 기록: 1초에 한 칸 (같은 초 안에서는 최대값 유지)
      const now = new Date(), sec = Math.floor(now.getTime() / 1000);
      const d = s.detections || {}, objs = s.objects || [];
      const cp = objs.filter((o) => o.cls === "person").length, cv = objs.length - cp;
      const lastRow = live[live.length - 1];
      if (lastRow && lastRow.sec === sec) {
        lastRow.p = Math.max(lastRow.p, d.person || 0); lastRow.v = Math.max(lastRow.v, d.vehicle || 0); lastRow.cp = cp; lastRow.cv = cv;
      } else {
        live.push({ sec, label: hms(now), p: d.person || 0, v: d.vehicle || 0, cp, cv });
        if (live.length > 3600) live.shift();
      }
    }
    const key = App.replay ? "r:" + App.replay.item.id : "live";
    if (key !== liveKey) { liveKey = key; drawnKey = null; }
    const t = Date.now();
    // 리플레이는 데이터가 고정이라 한 번만 그리고 재생선만 움직임, 실시간은 2초마다 다시 그림
    if (App.replay) {
      const sr = replaySeries();
      const k2 = cacheId;
      if (sr && drawnKey !== k2) { draw(sr); drawnKey = k2; }
      else if (!sr) draw(null);
      moveHead();
    } else if (t - lastDraw > 2000) { lastDraw = t; draw(liveSeries()); }
  });
})();
