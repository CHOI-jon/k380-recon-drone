// =========================================================
// ⑥ 비행 기록 — 비행 시작(ARM)~종료(DISARM) 구간의 기체 로그 + 객체인식 목록
//   비행 중에는 기록만 하고, 비행이 끝나면 목록을 표시한다.
//   app.js / features.js 수정 없이 App.on() / App.api() 만 사용
// =========================================================
(() => {
  const el = id => document.getElementById(id);
  const fmtDur = s => {
    s = Math.max(0, Math.round(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    const p = n => String(n).padStart(2, "0");
    return h ? `${h}:${p(m)}:${p(x)}` : `${p(m)}:${p(x)}`;
  };
  const v = (x, unit = "", nd) => (x == null ? "-" : (nd != null ? Number(x).toFixed(nd) : x) + unit);

  let flights = [];          // 요약 목록 (최신순)
  let shown = null;          // 현재 표시 중인 비행 전체 기록
  let lastStatus = null;
  let tab = "flTel";

  // ---------------- 상태 표시 (비행 중 / 대기)
  function renderStatus(st) {
    const badge = el("flBadge"), live = el("flLive");
    el("flStart").disabled = st.recording;
    el("flStop").disabled = !st.recording;
    if (st.recording) {
      badge.className = "fl-badge rec";
      badge.textContent = `● 기록 중 ${fmtDur(st.elapsed)}`;
      live.hidden = false;
      live.innerHTML =
        `<b>비행 중</b> — 기체 로그 <b>${st.tel_n}</b>건 · 탐지 <b>${st.det_n}</b>건 기록 중` +
        `<br><span class="muted small">비행이 끝나면(DISARM 또는 '비행 종료') 이번 비행의 기체 로그와 객체인식 목록이 아래에 표시됩니다.</span>`;
    } else {
      badge.className = "fl-badge";
      badge.textContent = st.auto ? "대기 — ARM 시 자동 기록" : "대기 — 수동 기록 모드";
      live.hidden = true;
    }
    // 비행 중에는 목록을 숨기고, 비행이 끝난 뒤에만 표시
    el("flBody").hidden = st.recording || !shown;
    el("flEmpty").hidden = st.recording || !!shown;
  }

  // ---------------- 지난 비행 선택
  function renderSelect() {
    const sel = el("flSelect");
    if (!flights.length) {
      sel.innerHTML = `<option value="">저장된 비행 없음</option>`;
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    sel.innerHTML = flights.map(f =>
      `<option value="${esc(f.id)}">${esc(f.start.slice(5, 16))} · ${fmtDur(f.duration_s)} · 사람 ${f.persons}/차량 ${f.vehicles}</option>`
    ).join("");
    if (shown) sel.value = shown.summary.id;
  }

  // ---------------- 비행 1건 표시
  function renderFlight() {
    const empty = el("flEmpty"), body = el("flBody");
    const has = !!shown, rec = !!(lastStatus && lastStatus.recording);
    empty.hidden = rec || has;
    body.hidden = rec || !has;
    el("flCsv").disabled = el("flJson").disabled = !has;
    if (!has) return;
    const s = shown.summary;
    const tile = (label, value) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`;
    el("flTiles").innerHTML =
      tile("비행 시간", fmtDur(s.duration_s)) +
      tile("최대 고도", v(s.max_alt_m, " m")) +
      tile("최대 속도", v(s.max_speed_ms, " m/s")) +
      tile("이동 거리", v(s.dist_m, " m")) +
      tile("배터리", s.battery_start == null ? "-" : `${s.battery_start} → ${v(s.battery_end, "%")}`) +
      tile("탐지 객체 (사람 / 차량)", `${s.persons} / ${s.vehicles}`);
    el("flMeta").textContent =
      `${s.start} ~ ${s.end.slice(11)} · 시작: ${s.reason_start} · 종료: ${s.reason_end} · 기체 로그 ${s.tel_n}건 · 탐지 ${s.det_n}건`;

    // 기체 로그
    const tel = shown.telemetry;
    el("flTelBody").innerHTML = tel.length ? tel.map(r => `<tr>
        <td>${r.time}</td><td>${fmtDur(r.t_s)}</td><td>${esc(r.mode)}</td>
        <td>${v(r.alt_m)}</td><td>${v(r.speed_ms)}</td><td>${v(r.battery_pct, "%")}</td><td>${v(r.voltage_v)}</td>
        <td>${esc(r.gps)} (${r.sats})</td><td>${v(r.lat)}</td><td>${v(r.lon)}</td>
        <td>${v(r.heading_deg, "°")}</td><td>${v(r.roll_deg)} / ${v(r.pitch_deg)}</td></tr>`).join("")
      : `<tr><td colspan="12" class="muted">기체 로그가 없습니다.</td></tr>`;

    // 객체인식 목록 (객체별)
    const objs = shown.objects;
    el("flObjBody").innerHTML = objs.length ? objs.map(o => `<tr>
        <td>${o.snap ? `<img class="fl-thumb" src="${esc(o.snap)}" alt="">` : ""}</td>
        <td><b>${esc(o.id)}</b></td><td><span class="fl-cls ${o.cls}">${CLS_KO[o.cls] || esc(o.cls)}</span></td>
        <td>${v(o.conf, "", 2)}</td><td>${v(o.lat)}</td><td>${v(o.lon)}</td><td>${v(o.err)}</td>
        <td>${o.hits}</td><td>${o.first_seen}</td><td>${o.last_seen}</td></tr>`).join("")
      : `<tr><td colspan="10" class="muted">이번 비행에서 탐지된 객체가 없습니다.</td></tr>`;

    // 개별 탐지 이벤트 (화면에는 최근 1000건, 전체는 CSV)
    const det = shown.detections, LIM = 1000;
    const rows = det.slice(-LIM);
    el("flDetNote").textContent = det.length > LIM ? `전체 ${det.length}건 중 마지막 ${LIM}건 표시 — 전체는 CSV 저장` : `${det.length}건`;
    el("flDetBody").innerHTML = rows.length ? rows.map(d => `<tr>
        <td>${d.time}</td><td>${fmtDur(d.t_s)}</td><td>${esc(d.obj_id || "-")}</td>
        <td><span class="fl-cls ${d.cls}">${CLS_KO[d.cls] || esc(d.cls)}</span></td><td>${v(d.conf, "", 2)}</td>
        <td>${v(d.lat)}</td><td>${v(d.lon)}</td><td>${v(d.err_m)}</td><td>${v(d.drone_alt_m)}</td><td>${esc(d.geo_mode)}</td></tr>`).join("")
      : `<tr><td colspan="10" class="muted">탐지 이벤트가 없습니다.</td></tr>`;

    el("flObjTabBtn").textContent = `객체인식 목록 (${objs.length})`;
    el("flTelTabBtn").textContent = `기체 로그 (${tel.length})`;
    el("flDetTabBtn").textContent = `탐지 이벤트 (${det.length})`;
  }

  function showTab(id) {
    tab = id;
    document.querySelectorAll("#flightCard .fl-tab").forEach(b => b.classList.toggle("active", b.dataset.fl === id));
    ["flTel", "flObj", "flDet"].forEach(k => (el(k).hidden = k !== id));
  }

  // ---------------- 서버 통신
  function loadList(selectId) {
    return App.api("/api/flights").then(r => {
      flights = r.flights;
      lastStatus = r.status;
      renderStatus(r.status);
      const id = selectId || (flights[0] && flights[0].id);
      renderSelect();
      return id ? loadFlight(id) : null;
    });
  }
  function loadFlight(id) {
    return App.api(`/api/flights/${encodeURIComponent(id)}`).then(rec => {
      shown = rec;
      renderSelect();
      renderFlight();
    });
  }

  // ---------------- CSV / JSON 저장
  const CSV = {
    flTel: { name: "telemetry", cols: ["time", "t_s", "mode", "armed", "alt_m", "speed_ms", "battery_pct", "voltage_v", "gps", "sats", "lat", "lon", "heading_deg", "roll_deg", "pitch_deg"], rows: () => shown.telemetry },
    flObj: { name: "objects", cols: ["id", "cls", "conf", "lat", "lon", "err", "hits", "first_seen", "last_seen"], rows: () => shown.objects },
    flDet: { name: "detections", cols: ["time", "t_s", "obj_id", "cls", "conf", "lat", "lon", "err_m", "drone_lat", "drone_lon", "drone_alt_m", "geo_mode"], rows: () => shown.detections },
  };
  el("flCsv").addEventListener("click", () => {
    if (!shown) return;
    const c = CSV[tab];
    const cell = x => (x == null ? "" : /[",\n]/.test(String(x)) ? `"${String(x).replace(/"/g, '""')}"` : x);
    const text = c.cols.join(",") + "\n" + c.rows().map(r => c.cols.map(k => cell(r[k])).join(",")).join("\n");
    downloadText(`flight_${shown.summary.id}_${c.name}.csv`, text, "text/csv");
  });
  el("flJson").addEventListener("click", () => {
    if (shown) downloadText(`flight_${shown.summary.id}.json`, JSON.stringify(shown, null, 1), "application/json");
  });

  el("flStart").addEventListener("click", () =>
    App.api("/api/flight/start", { method: "POST" }).then(r => renderStatus(r.status)).catch(e => alertMsg(e)));
  el("flStop").addEventListener("click", () =>
    App.api("/api/flight/stop", { method: "POST" }).then(r => loadList(r.summary.id)).catch(e => alertMsg(e)));
  el("flSelect").addEventListener("change", e => e.target.value && loadFlight(e.target.value));
  document.querySelectorAll("#flightCard .fl-tab").forEach(b => b.addEventListener("click", () => showTab(b.dataset.fl)));

  function alertMsg(e) { el("flMeta").textContent = "오류: " + e.message; }

  // ---------------- 실시간 상태 → 비행이 끝나는 순간 목록 갱신
  App.on(s => {
    const st = s.flight;
    if (!st) return;
    const ended = lastStatus && lastStatus.recording && !st.recording;
    const newLast = lastStatus && st.last_id && st.last_id !== lastStatus.last_id;
    lastStatus = st;
    renderStatus(st);
    if (ended || newLast) loadList(st.last_id).catch(() => {});
  });

  showTab("flTel");
  App.ready.then(() => loadList()).catch(() => {});
})();
