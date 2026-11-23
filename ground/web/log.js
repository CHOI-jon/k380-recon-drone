// 비행 로그 분석 화면 — /lib/<id>/log.json 을 표·그래프로 보여 준다
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const id = new URLSearchParams(location.search).get("id");
  const MODE_CLR = { STABILIZE: "#64748b", ALT_HOLD: "#0ea5e9", LOITER: "#22c55e", AUTO: "#a855f7", RTL: "#f97316",
                     LAND: "#ef4444", GUIDED: "#eab308", POSHOLD: "#14b8a6", SMART_RTL: "#fb923c" };
  const tip = $("lgTip");
  let L = null;

  fetch(`/lib/${encodeURIComponent(id)}/log.json?v=${Date.now()}`).then((r) => {
    if (!r.ok) throw new Error("분석 결과가 없습니다 — 보관함에서 분석이 끝났는지 확인하세요");
    return r.json();
  }).then((j) => { L = j; render(); }).catch((e) => { $("lgMeta").textContent = e.message; });

  const fmtDur = (s) => `${Math.floor(s / 60)}분 ${Math.round(s % 60)}초`;
  const vcls = (v) => (v == null ? "" : v < 15 ? "v-good" : v < 30 ? "v-ok" : "v-bad");

  function render() {
    document.title = `비행 로그 분석 — ${L.file}`;
    $("lgTitle").textContent = `비행 로그 분석 — ${L.file}`;
    $("lgMeta").textContent = `${L.start || "시각 정보 없음"} · 기록 길이 ${fmtDur(L.length)} · 비행 구간 ${L.segments.length}개 · 모드 ${[...new Set(L.modes.map((m) => m[1]))].join(", ") || "-"}`;
    $("lgAdv").innerHTML = L.advice.map(([lv, t]) => `<li class="${lv}">${esc(t)}</li>`).join("") || "<li class='good'>특이사항 없음</li>";

    $("lgSegs").innerHTML = L.segments.map((s) => {
      const vm = s.vibe_mean || [], vx = s.vibe_max || [];
      const blur = s.blur ? `${s.blur[1][1]} px · ${s.blur[4][1]} px` : "-";
      return `<tr><td>${s.no}</td><td>${esc(s.start || s.t0 + " s")}</td><td>${fmtDur(s.dur)}</td><td class="wrap">${esc(s.modes.join(" → "))}</td>
        <td>${s.alt_max ?? "-"} m</td><td>${s.spd_max ?? "-"} m/s</td>
        <td>${vm.map((v) => `<span class="${vcls(v)}">${v}</span>`).join(" / ") || "-"}</td>
        <td>${vx.map((v) => `<span class="${vcls(v)}">${v}</span>`).join(" / ") || "-"}</td>
        <td class="${s.clip ? "v-bad" : ""}">${s.clip ?? "-"}</td>
        <td>${s.rate ? s.rate.join(" / ") + " °/s" : "-"}</td><td>${blur}</td>
        <td>${s.volt ? `${s.volt[0]} → ${s.volt[1]} V (${s.volt[2]})` : "-"}</td><td>${s.curr_mean ?? "-"} A</td>
        <td>${s.motors && s.motors.length ? s.motors.join(" / ") + ` (차이 ${s.motor_spread})` : "-"}</td></tr>`;
    }).join("") || `<tr><td colspan="14" class="muted">공중에 떠 있던 구간이 없습니다</td></tr>`;

    const S = L.series, X = L.length;
    const charts = [
      { title: "고도 (m)", series: [{ name: "상대고도", color: "#facc15", data: S.alt.map((r) => [r[0], r[1]]) }] },
      { title: "속도 (m/s) · GPS 위성 수", series: [{ name: "지면속도", color: "#38bdf8", data: S.gps.map((r) => [r[0], r[1]]) },
                                                { name: "위성 수", color: "#a3a3a3", data: S.gps.map((r) => [r[0], r[2]]) }] },
      { title: "진동 (m/s²)", hlines: [[15, "좋음"], [30, "점검"]], series: [
        { name: "X 앞뒤", color: "#38bdf8", data: S.vibe.map((r) => [r[0], r[1]]) },
        { name: "Y 좌우", color: "#f43f5e", data: S.vibe.map((r) => [r[0], r[2]]) },
        { name: "Z 상하", color: "#4ade80", data: S.vibe.map((r) => [r[0], r[3]]) }] },
      { title: "기체 회전속도 (°/s, 롤·피치)", series: [{ name: "회전속도", color: "#f97316", data: S.rate.map((r) => [r[0], r[1]]) }] },
      { title: "배터리 전압 (V)", series: [{ name: "전압", color: "#4ade80", data: S.bat.map((r) => [r[0], r[1]]) }] },
      { title: "스로틀 출력 (0~1) · 배터리 전류 (A)", series: [{ name: "스로틀", color: "#e879f9", data: S.thr.map((r) => [r[0], r[1]]) },
                                                      { name: "전류", color: "#fbbf24", data: S.bat.map((r) => [r[0], r[2]]) }] },
    ];
    const box = $("lgCharts");
    box.innerHTML = charts.map((c, i) => `<div class="card"><div class="card-header"><h2>${esc(c.title)}</h2>
      <div class="lg-legend">${c.series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div></div>
      <div class="lg-chart"><canvas id="lgC${i}"></canvas></div></div>`).join("");
    charts.forEach((c, i) => chart($(`lgC${i}`), Object.assign({ xmax: X, bands: modeBands(), xLabel: "s" }, c)));

    if (L.fft && L.fft.f) {
      $("lgFftCard").hidden = false;
      $("lgFftMeta").textContent = `샘플링 ${L.fft.rate} Hz · ${L.fft.batches}개 묶음 평균 · 최고점 ${L.fft.peak ? Math.round(L.fft.peak) + " Hz" : "-"}`;
      const f = L.fft.f;
      chart($("lgFft"), { xmax: f[f.length - 1], xLabel: "Hz", vlines: L.fft.peak ? [[L.fft.peak, Math.round(L.fft.peak) + " Hz"]] : [],
        series: [{ name: "X", color: "#38bdf8", data: f.map((v, k) => [v, L.fft.x[k]]) },
                 { name: "Y", color: "#f43f5e", data: f.map((v, k) => [v, L.fft.y[k]]) },
                 { name: "Z", color: "#4ade80", data: f.map((v, k) => [v, L.fft.z[k]]) }] });
    }

    $("lgEvents").innerHTML = L.events.sort((a, b) => a[0] - b[0]).map((e) =>
      `<tr><td>${e[0]}</td><td class="${e[1] === "오류" ? "v-bad" : ""}">${esc(e[1])}</td><td class="wrap">${esc(e[2])}</td></tr>`).join("")
      || `<tr><td colspan="3" class="muted">없음</td></tr>`;
    $("lgParams").innerHTML = Object.entries(L.params).map(([k, v]) =>
      `<div><span>${esc(k)}</span> ${Math.round(v * 1000) / 1000}</div>`).join("");
  }

  function modeBands() {
    const out = [], M = L.modes;
    for (let i = 0; i < M.length; i++) {
      const a = M[i][0], b = i + 1 < M.length ? M[i + 1][0] : L.length;
      if (out.length && out[out.length - 1].name === M[i][1]) { out[out.length - 1].b = b; continue; }
      out.push({ a, b, name: M[i][1] });
    }
    return out;
  }

  // ---- 간단한 선 그래프 (확대 없음, 마우스를 올리면 값 표시)
  function chart(cv, o) {
    const dpr = window.devicePixelRatio || 1;
    const draw = (hx) => {
      const W = cv.clientWidth, H = cv.clientHeight;
      cv.width = W * dpr; cv.height = H * dpr;
      const g = cv.getContext("2d");
      g.scale(dpr, dpr);
      const pad = { l: 44, r: 10, t: o.bands ? 18 : 8, b: 22 };
      let lo = Infinity, hi = -Infinity;
      for (const s of o.series) for (const [, y] of s.data) if (y != null) { lo = Math.min(lo, y); hi = Math.max(hi, y); }
      for (const [y] of o.hlines || []) hi = Math.max(hi, y * 1.05);
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      if (lo > 0 && lo < hi * 0.35) lo = 0;
      const span = hi - lo || 1;
      lo -= span * 0.05; hi += span * 0.05;
      const X = (x) => pad.l + (x / (o.xmax || 1)) * (W - pad.l - pad.r);
      const Y = (y) => pad.t + (1 - (y - lo) / (hi - lo)) * (H - pad.t - pad.b);
      g.clearRect(0, 0, W, H);
      g.font = "11px 'Malgun Gothic', sans-serif";
      // 모드 띠
      for (const b of o.bands || []) {
        const c = MODE_CLR[b.name] || "#94a3b8";
        g.fillStyle = c + "26"; g.fillRect(X(b.a), pad.t, X(b.b) - X(b.a), H - pad.t - pad.b);
        g.fillStyle = c; g.fillRect(X(b.a), 2, Math.max(1, X(b.b) - X(b.a) - 1), 12);
        if (X(b.b) - X(b.a) > 44) { g.fillStyle = "#0f172a"; g.fillText(b.name, X(b.a) + 3, 12); }
      }
      // 눈금
      g.strokeStyle = "rgba(255,255,255,0.08)"; g.fillStyle = "#94a3b8"; g.lineWidth = 1;
      for (let k = 0; k <= 4; k++) {
        const y = lo + ((hi - lo) * k) / 4;
        g.beginPath(); g.moveTo(pad.l, Y(y)); g.lineTo(W - pad.r, Y(y)); g.stroke();
        g.fillText(Math.abs(y) >= 100 ? y.toFixed(0) : y.toFixed(1), 4, Y(y) + 4);
      }
      const step = niceStep((o.xmax || 1) / 6);
      for (let x = 0; x <= (o.xmax || 1); x += step) g.fillText(`${Math.round(x)}${o.xLabel || ""}`, X(x) - 10, H - 6);
      for (const [y, lab] of o.hlines || []) {
        g.strokeStyle = "rgba(251,191,36,0.6)"; g.setLineDash([4, 4]);
        g.beginPath(); g.moveTo(pad.l, Y(y)); g.lineTo(W - pad.r, Y(y)); g.stroke(); g.setLineDash([]);
        g.fillStyle = "#fbbf24"; g.fillText(lab, W - pad.r - 30, Y(y) - 3);
      }
      for (const [x, lab] of o.vlines || []) {
        g.strokeStyle = "rgba(250,204,21,0.8)"; g.setLineDash([4, 4]);
        g.beginPath(); g.moveTo(X(x), pad.t); g.lineTo(X(x), H - pad.b); g.stroke(); g.setLineDash([]);
        g.fillStyle = "#facc15"; g.fillText(lab, X(x) + 4, pad.t + 12);
      }
      for (const s of o.series) {
        g.strokeStyle = s.color; g.lineWidth = 1.6; g.beginPath();
        let pen = false;
        for (const [x, y] of s.data) {
          if (y == null) { pen = false; continue; }
          if (!pen) { g.moveTo(X(x), Y(y)); pen = true; } else g.lineTo(X(x), Y(y));
        }
        g.stroke();
      }
      if (hx != null) {
        g.strokeStyle = "rgba(255,255,255,0.4)"; g.beginPath(); g.moveTo(hx, pad.t); g.lineTo(hx, H - pad.b); g.stroke();
      }
      return { X, Y, pad, W };
    };
    let geo = draw();
    new ResizeObserver(() => (geo = draw())).observe(cv);
    cv.addEventListener("mousemove", (e) => {
      const r = cv.getBoundingClientRect(), mx = e.clientX - r.left;
      const x = ((mx - geo.pad.l) / (geo.W - geo.pad.l - geo.pad.r)) * (o.xmax || 1);
      if (x < 0 || x > (o.xmax || 1)) { tip.hidden = true; draw(); return; }
      geo = draw(mx);
      const rows = o.series.map((s) => {
        let best = null, bd = Infinity;
        for (const p of s.data) { const d = Math.abs(p[0] - x); if (d < bd) { bd = d; best = p; } }
        return best && best[1] != null ? `<span style="color:${s.color}">●</span> ${esc(s.name)} ${Math.round(best[1] * 100) / 100}` : "";
      }).filter(Boolean);
      const band = (o.bands || []).find((b) => x >= b.a && x < b.b);
      tip.innerHTML = `<b>${x.toFixed(1)}${o.xLabel || ""}</b>${band ? " · " + band.name : ""}<br>${rows.join("<br>")}`;
      tip.hidden = false;
      tip.style.left = Math.min(window.innerWidth - 180, e.clientX + 14) + "px";
      tip.style.top = e.clientY + 14 + "px";
    });
    cv.addEventListener("mouseleave", () => { tip.hidden = true; geo = draw(); });
  }

  function niceStep(v) {
    const p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
    return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * p;
  }
})();
