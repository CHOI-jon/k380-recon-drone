// 감시구역 현황 카드 — 구역별 상태판 + 침입 객체 카드 + 침입 타임라인
// 실시간(지상국 판정)과 리플레이(replay.js 판정) 모두 같은 state.alerts / objects.zones 를 쓴다
(function () {
  if (typeof App === "undefined" || !App.ready) return;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const KO = { person: "사람", vehicle: "차량" };
  let lastSig = "", lastDraw = 0, layers = null;

  const hms = (s) => { const m = /(\d{1,2}):(\d{2}):(\d{2})/.exec(s || ""); return m ? +m[1] * 3600 + +m[2] * 60 + +m[3] : null; };
  const dur = (sec) => (sec == null || sec < 0 ? "-" : sec < 60 ? `${Math.round(sec)}초` : `${Math.floor(sec / 60)}분 ${Math.round(sec % 60)}초`);
  const video = () => document.querySelector("video.demo-video");

  function seek(t) {
    const v = video();
    if (App.replay && v && t != null) { v.currentTime = Math.max(0, t - 1); v.pause(); }
  }

  function render(s) {
    const zones = App.zones || [];
    const alerts = s.alerts || [];
    const objs = Object.fromEntries((s.objects || []).map((o) => [o.id, o]));
    const nowSec = hms(s.objects && s.objects.length ? s.objects.reduce((a, o) => (o.last_seen > a ? o.last_seen : a), "") : "") ?? 0;

    $("zsMeta").textContent = zones.length ? `구역 ${zones.length}개 · 누적 침입 ${alerts.length}건` : "구역 없음";
    $("zsEmpty").hidden = zones.length > 0;
    $("zsBody").hidden = zones.length === 0;
    if (!zones.length) return;

    // ① 구역별 상태판
    $("zsZones").innerHTML = zones.map((z) => {
      const inside = Object.values(objs).filter((o) => o.zones.includes(z.id) && o.active);
      const za = alerts.filter((a) => a.zone_id === z.id);
      const p = inside.filter((o) => o.cls === "person").length, v = inside.length - p;
      const on = inside.length > 0;
      return `<div class="zs-zone ${on ? "alarm" : ""}" data-zone="${esc(z.id)}">
        <span class="zs-dot"></span><b class="zs-name">${esc(z.name)}</b>
        <span class="zs-state">${on ? "침입 중" : "이상 없음"}</span>
        <span class="zs-cell"><em>현재</em> 사람 <b>${p}</b> · 차량 <b>${v}</b></span>
        <span class="zs-cell"><em>누적 침입</em> <b>${za.length}</b></span>
        <span class="zs-cell"><em>마지막</em> ${za.length ? esc(za[za.length - 1].time) : "-"}</span>
      </div>`;
    }).join("");

    // ② 침입 객체 카드 (최근 순)
    const cards = [...alerts].reverse().map((a) => {
      const o = objs[a.obj_id];
      const enter = hms(a.time), last = o ? hms(o.last_seen) : null;
      const stay = enter != null && last != null ? last - enter : null;
      const active = o && o.active && o.zones.includes(a.zone_id);
      return `<button class="zs-card ${a.cls} ${active ? "now" : ""}" data-id="${esc(a.obj_id)}" data-t="${a.t ?? ""}" type="button">
        ${o && o.snap ? `<img src="${esc(o.snap)}" alt="${esc(a.obj_id)}">` : `<div class="zs-noimg">${a.cls === "person" ? "P" : "V"}</div>`}
        <div class="zs-info">
          <div><b>${esc(a.obj_id)}</b> <span class="tag ${a.cls}">${KO[a.cls]}</span> ${active ? '<span class="zs-live">체류 중</span>' : ""}</div>
          <div class="zs-z">⚠ ${esc(a.zone)}</div>
          <div class="muted small">진입 ${esc(a.time)} · ${active ? "머문 시간" : "머문 시간(마지막 탐지까지)"} ${dur(stay)}</div>
        </div>
      </button>`;
    });
    $("zsCards").innerHTML = cards.join("") || `<p class="muted small">아직 구역에 들어온 객체가 없습니다.</p>`;

    // ③ 침입 타임라인
    const tl = $("zsTimeline");
    const v = video();
    let t0, t1, pos;
    if (App.replay && v && v.duration) { t0 = 0; t1 = v.duration; pos = (a) => a.t; }
    else {
      const xs = alerts.map((a) => hms(a.time)).filter((x) => x != null);
      if (!xs.length) { tl.innerHTML = ""; tl.hidden = true; return; }
      t0 = Math.min(...xs) - 30; t1 = Math.max(nowSec, ...xs) + 30; pos = (a) => hms(a.time);
    }
    tl.hidden = false;
    const pct = (x) => Math.max(0, Math.min(100, ((x - t0) / (t1 - t0 || 1)) * 100));
    const head = App.replay && v ? `<i class="zs-head" style="left:${pct(v.currentTime)}%"></i>` : "";
    tl.innerHTML = `<div class="zs-track">${alerts.map((a) => pos(a) == null ? "" :
      `<button class="zs-mark ${a.cls}" style="left:${pct(pos(a))}%" data-id="${esc(a.obj_id)}" data-t="${a.t ?? ""}" title="${esc(a.time)} · ${esc(a.zone)} · ${esc(a.obj_id)}"></button>`).join("")}${head}</div>
      <div class="zs-axis"><span>${App.replay ? "영상 시작" : "첫 침입 전"}</span><span>${App.replay ? "영상 끝" : "지금"}</span></div>`;
  }

  function onClick(e) {
    const b = e.target.closest("[data-id]");
    if (!b) return;
    if (b.dataset.t) seek(parseFloat(b.dataset.t));
    App.focusObject(b.dataset.id);
  }
  $("zsCards").addEventListener("click", onClick);
  $("zsTimeline").addEventListener("click", onClick);
  $("zsZones").addEventListener("click", (e) => {
    const r = e.target.closest("[data-zone]");
    const z = r && (App.zones || []).find((x) => x.id === r.dataset.zone);
    if (z) App.map.fitBounds(L.latLngBounds(z.points).pad(0.3), { maxZoom: 20 });
  });
  $("zsManage").addEventListener("click", () => { const b = $("zoneToggle"); if (b) b.click(); });
  $("zsManage2").addEventListener("click", () => { const b = $("zoneToggle"); if (b) b.click(); });

  App.on((s) => {
    const zones = App.zones || [];
    const sig = zones.map((z) => z.id).join() + "|" + (s.alerts || []).map((a) => a.seq).join() + "|" +
      (s.objects || []).filter((o) => o.zones.length).map((o) => o.id + o.active + o.last_seen + (o.snap || "")).join();
    const now = Date.now();
    if (sig === lastSig && now - lastDraw < 1000) return;
    lastSig = sig; lastDraw = now;
    render(s);
  });
})();
