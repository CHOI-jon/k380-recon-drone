// 정찰 상황 일지 — 비행 중 일어난 일을 시간순으로 (식별·구역 침입·이탈·동시 탐지 최대·기체 상태)
// 리플레이: replay.js가 영상 전체 사건을 계산 → 재생 시각까지만 표시, 누르면 그 시각으로 이동
// 실시간: 지상국 상태가 바뀔 때마다 사건으로 기록 (대시보드를 연 뒤부터)
// App.eventLog() : 보고서용 사건 목록
(function () {
  if (typeof App === "undefined" || !App.ready) return;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const KO = { person: "사람", vehicle: "차량" };
  const ICON = { flight: "✈", obj: "◎", alert: "⚠", left: "✕", peak: "▲", link: "⚡", batt: "🔋" };
  const NAME = { flight: "기체", obj: "식별", alert: "구역 침입", left: "이탈", peak: "최대 탐지", link: "연결", batt: "배터리" };
  let filter = "all", replayKey = null, replayEv = [], lastSig = "", lastDraw = 0;

  // ---- 실시간 사건 기록
  const live = [];
  const prev = { ids: new Set(), alerts: new Set(), armed: null, mode: null, batt: null, pi: null, fc: null, mp: 1, mv: 1, init: false };
  const hms = () => { const d = new Date(), p = (n) => String(n).padStart(2, "0"); return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };
  function addLive(type, text, extra) {
    live.push(Object.assign({ t: Date.now() / 1000, time: hms(), type, text }, extra || {}));
    if (live.length > 1000) live.shift();
  }
  function trackLive(s) {
    const objs = s.objects || [];
    if (!prev.init) {                       // 처음 받은 상태는 기준으로만 (이미 있던 객체를 '새 식별'로 쏟아내지 않음)
      prev.init = true;
      objs.forEach((o) => prev.ids.add(o.id));
      (s.alerts || []).forEach((a) => prev.alerts.add(a.seq));
      prev.armed = s.armed; prev.mode = s.mode; prev.batt = s.battery;
      prev.pi = s.link && s.link.pi; prev.fc = s.link && s.link.fc;
      addLive("flight", `대시보드 연결 — ${s.armed ? "비행 중" : "지상 대기"}${s.mode ? " · " + s.mode : ""}`);
      return;
    }
    for (const o of objs) if (!prev.ids.has(o.id)) { prev.ids.add(o.id); addLive("obj", `${KO[o.cls]} ${o.id} 식별`, { id: o.id, cls: o.cls, snap: o.snap }); }
    for (const a of s.alerts || []) if (!prev.alerts.has(a.seq)) { prev.alerts.add(a.seq); addLive("alert", `${a.zone} 침입 — ${KO[a.cls]} ${a.obj_id}`, { id: a.obj_id, cls: a.cls }); }
    if (s.armed !== prev.armed && s.armed != null) { addLive("flight", s.armed ? "ARM — 비행 시작" : "DISARM — 비행 종료"); prev.armed = s.armed; }
    if (s.mode && s.mode !== prev.mode) { if (prev.mode) addLive("flight", `비행 모드 → ${s.mode}`); prev.mode = s.mode; }
    if (s.battery != null && prev.batt != null) {
      for (const th of [50, 30, 20]) if (prev.batt > th && s.battery <= th) addLive("batt", `배터리 ${th}% 이하 (${s.battery}%)`);
    }
    if (s.battery != null) prev.batt = s.battery;
    const L = s.link || {};
    if (L.pi !== prev.pi && L.pi != null) { addLive("link", L.pi ? "Pi 영상·탐지 연결 복구" : "Pi 영상·탐지 연결 끊김", { warn: !L.pi }); prev.pi = L.pi; }
    if (L.fc !== prev.fc && L.fc != null) { addLive("link", L.fc ? "기체 텔레메트리 연결 복구" : "기체 텔레메트리 끊김", { warn: !L.fc }); prev.fc = L.fc; }
    const d = s.detections || {};
    if ((d.person || 0) > prev.mp) { prev.mp = d.person; addLive("peak", `동시 탐지 최대 갱신 — 사람 ${d.person}명`, { cls: "person" }); }
    if ((d.vehicle || 0) > prev.mv) { prev.mv = d.vehicle; addLive("peak", `동시 탐지 최대 갱신 — 차량 ${d.vehicle}대`, { cls: "vehicle" }); }
  }

  const video = () => document.querySelector("video.demo-video");
  function current() {
    if (App.replay && App.replay.events) {
      const key = App.replay.item.id + "|" + (video() && video().duration ? Math.round(video().duration) : 0);
      if (key !== replayKey) { replayKey = key; replayEv = App.replay.events(); }
      const v = video(), t = v ? v.currentTime : Infinity;
      return replayEv.filter((e) => e.t <= t + 0.05);
    }
    replayKey = null;
    return live;
  }
  App.eventLog = (tCut) => {
    const ev = App.replay && App.replay.events ? App.replay.events(tCut) : live;
    return ev.map((e) => ({ time: e.time, type: e.type, text: e.text }));
  };

  // ---- 요약 숫자 (정찰 범위 포함)
  const fmtArea = (m2) => (m2 >= 10000 ? `${(m2 / 10000).toFixed(2)} ha` : `${Math.round(m2).toLocaleString()} m²`);
  const fmtDist = (m) => (m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`);
  const fmtDur = (s) => `${Math.floor(s / 60)}분 ${String(Math.round(s % 60)).padStart(2, "0")}초`;
  function renderStats(s) {
    const c = App.coverageStats ? App.coverageStats() : { area_m2: 0, path_m: 0, dur_s: 0 };
    const objs = s.objects || [];
    const p = objs.filter((o) => o.cls === "person").length, v = objs.length - p;
    const left = objs.filter((o) => o.left).length;
    const per = c.area_m2 > 0 ? (objs.length / (c.area_m2 / 10000)).toFixed(1) : "-";
    const tile = (k, val, hint) => `<div class="metric"><span>${k}</span><strong>${val}</strong>${hint ? `<em>${hint}</em>` : ""}</div>`;
    $("evStats").innerHTML =
      tile("정찰 면적", c.area_m2 ? fmtArea(c.area_m2) : "-", "카메라가 촬영한 땅 (지도의 하늘색)") +
      tile("비행 거리", c.path_m ? fmtDist(c.path_m) : "-", c.dur_s ? `비행 시간 ${fmtDur(c.dur_s)}` : "") +
      tile("식별 객체", `${p} / ${v}`, "사람 / 차량") +
      tile("면적당 식별", per === "-" ? "-" : `${per}개/ha`, "1 ha = 100 m × 100 m") +
      tile("구역 침입", `${(s.alerts || []).length}건`, "") +
      tile("이탈 확인", App.replay ? `${left}개` : "-", App.replay ? "시야 안에서 사라진 객체" : "리플레이에서 표시");
  }

  function renderList(ev) {
    const list = ev.filter((e) => filter === "all" || e.type === filter || (filter === "flight" && (e.type === "link" || e.type === "batt")));
    $("evMeta").textContent = `사건 ${ev.length}건` + (App.replay ? " · 재생 시각까지" : " · 대시보드 연결 후");
    $("evList").innerHTML = list.length ? [...list].reverse().slice(0, 400).map((e) => `
      <li class="ev ev-${e.type}${e.warn ? " warn" : ""}" ${e.id ? `data-id="${esc(e.id)}"` : ""} ${App.replay ? `data-t="${e.t}"` : ""}>
        <span class="ev-time">${esc(e.time)}</span>
        <span class="ev-ic">${ICON[e.type] || "·"}</span>
        <span class="ev-kind">${NAME[e.type] || ""}</span>
        <span class="ev-text">${e.cls ? `<i class="ev-dot ${e.cls}"></i>` : ""}${esc(e.text)}</span>
        ${e.snap ? `<img class="ev-snap" src="${esc(e.snap)}" alt="">` : ""}
      </li>`).join("") : `<li class="ev-empty muted">${App.replay ? "재생하면 사건이 여기에 쌓입니다." : "아직 기록된 사건이 없습니다."}</li>`;
  }

  $("evFilter").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-f]");
    if (!b) return;
    filter = b.dataset.f;
    document.querySelectorAll("#evFilter button").forEach((x) => x.classList.toggle("on", x === b));
    lastSig = ""; renderList(current());
  });
  $("evList").addEventListener("click", (e) => {
    const li = e.target.closest("li.ev");
    if (!li) return;
    const v = video();
    if (App.replay && v && li.dataset.t) { v.currentTime = Math.max(0, parseFloat(li.dataset.t) - 1); v.pause(); }
    if (li.dataset.id) setTimeout(() => App.focusObject(li.dataset.id), App.replay ? 400 : 0);
  });

  App.on((s) => {
    if (!App.replay) trackLive(s);
    const ev = current();
    const t = Date.now();
    const sig = ev.length + "|" + filter + "|" + (App.replay ? replayKey : "live");
    if (sig !== lastSig) { lastSig = sig; renderList(ev); }
    if (t - lastDraw > 1000) { lastDraw = t; renderStats(s); }
  });
})();
