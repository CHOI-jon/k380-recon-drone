// =========================================================
// ③ 성능 모니터  ④ 감시구역·경보  ① 정확도 검증  ⑤ 정찰 보고서
// =========================================================

// ---------------------------------------------------------------- ③ 성능
(() => {
  let hist = [];
  const WINDOW = 180;
  const THROTTLE_BITS = [[0, "저전압"], [1, "클럭 제한"], [2, "스로틀링"], [3, "온도 한계"]];

  function throttleText(hex) {
    if (hex == null) return { level: "unknown", text: "정보 없음" };
    const v = parseInt(hex, 16);
    if (isNaN(v)) return { level: "unknown", text: hex };
    const now = THROTTLE_BITS.filter(([b]) => v & (1 << b)).map(([, t]) => t);
    const past = THROTTLE_BITS.filter(([b]) => v & (1 << (b + 16))).map(([, t]) => t);
    if (now.length) return { level: "critical", text: `현재 ${now.join("·")}` };
    if (past.length) return { level: "warning", text: `과거 발생: ${past.join("·")}` };
    return { level: "good", text: "정상" };
  }
  const ICON = { good: "✔", warning: "▲", critical: "✖", unknown: "–" };

  function render() {
    const s = hist[hist.length - 1] || {};
    const th = throttleText(s.throttled);
    const cell = (label, value, level) =>
      `<div class="pstat ${level || ""}"><span>${label}</span><b>${level ? ICON[level] + " " : ""}${value}</b></div>`;
    $("perfStatus").innerHTML =
      cell("Pi 전원/발열", th.text, th.level) +
      cell("추론 시간", s.infer_ms != null ? `${s.infer_ms.toFixed(0)} ms` : "-") +
      cell("핫스팟 왕복", s.rtt_ms != null ? `${s.rtt_ms.toFixed(0)} ms` : "-") +
      cell("수신률 FC / Pi", `${s.fc_rate ?? "-"} / ${s.pi_rate ?? "-"} Hz`);
    const series = k => hist.map(h => ({ t: h.t, v: h[k] }));
    Charts.spark($("sparkFps"), { title: "추론 FPS", data: series("fps"), digits: 1, yMin: 0 });
    Charts.spark($("sparkE2e"), { title: "영상→화면 지연", unit: " ms", data: series("e2e_ms"), yMin: 0,
      warn: v => v > 500 });
    Charts.spark($("sparkTemp"), { title: "Pi 온도", unit: " °C", data: series("temp_c"), digits: 1,
      warn: v => v >= 80 });
    Charts.spark($("sparkCpu"), { title: "Pi CPU", unit: " %", data: series("cpu_pct"), yMin: 0, yMax: 100,
      warn: v => v > 95 });
  }

  App.ready.then(() => App.api("/api/perf")).then(r => { hist = r.history.slice(-WINDOW); render(); }).catch(() => {});
  // K380 녹화 리플레이: replay.js가 영상 시각까지의 파이 성능 기록을 넘겨줌
  App.perfCtl = { set(h) { hist = h.slice(-WINDOW); render(); }, live() { App.api("/api/perf").then(r => { hist = r.history.slice(-WINDOW); render(); }).catch(() => {}); } };
  App.on(s => {
    if (App.replay && App.replay.data && App.replay.data.perf) return;   // 리플레이 중에는 기록값 유지
    const p = s.perf;
    if (!p || (hist.length && hist[hist.length - 1].t === p.t)) return;
    hist.push(p);
    if (hist.length > WINDOW) hist.shift();
    render();
  });
})();

// ---------------------------------------------------------------- ④ 감시구역 + 경보
(() => {
  const layers = {};
  let draft = [], draftLine = null, draftDots = [];
  let lastAlertSeq = null, alertCount = 0;
  App.zones = [];

  function drawZones() {
    for (const id in layers) { App.map.removeLayer(layers[id]); delete layers[id]; }
    for (const z of App.zones) {
      layers[z.id] = L.polygon(z.points, { color: "#f97316", weight: 2, dashArray: "6 4", fillOpacity: 0.08 })
        .addTo(App.map).bindTooltip(z.name, { permanent: true, direction: "center", className: "zone-label" });
    }
    $("zoneList").innerHTML = App.zones.length
      ? App.zones.map(z => `<li><span>▱ ${esc(z.name)}</span>
          <span><button class="link" data-go="${z.id}">보기</button><button class="link danger" data-del="${z.id}">삭제</button></span></li>`).join("")
      : `<li class="muted small">설정된 구역 없음</li>`;
  }
  const load = () => App.api("/api/zones").then(z => { App.zones = z; drawZones(); });

  function setDrawing(on) {
    $("zoneIdle").hidden = on;
    $("zoneDrawing").hidden = !on;
    $("mapHint").hidden = !on;
    $("mapHint").textContent = "구역 그리기: 지도를 클릭해 꼭짓점 추가";
    $("map").classList.toggle("picking", on);
    if (!on) {
      draft = [];
      draftDots.forEach(d => App.map.removeLayer(d));
      draftDots = [];
      if (draftLine) App.map.removeLayer(draftLine);
      draftLine = null;
      App.mapClick = null;
    }
  }
  function redrawDraft() {
    if (draftLine) App.map.removeLayer(draftLine);
    draftLine = L.polygon(draft, { color: "#f97316", weight: 2, fillOpacity: 0.05 }).addTo(App.map);
    $("zoneVtx").textContent = draft.length;
  }

  $("zoneStart").addEventListener("click", () => {
    setDrawing(true);
    App.mapClick = ll => {
      draft.push([ll.lat, ll.lng]);
      draftDots.push(L.circleMarker(ll, { radius: 4, color: "#f97316" }).addTo(App.map));
      redrawDraft();
    };
  });
  $("zoneUndo").addEventListener("click", () => {
    draft.pop();
    const d = draftDots.pop();
    if (d) App.map.removeLayer(d);
    redrawDraft();
  });
  $("zoneCancel").addEventListener("click", () => setDrawing(false));
  $("zoneFinish").addEventListener("click", async () => {
    if (draft.length < 3) { $("mapHint").textContent = "꼭짓점이 3개 이상 필요합니다"; return; }
    try {
      await App.api("/api/zones", { method: "POST", body: JSON.stringify({ name: $("zoneName").value.trim(), points: draft }) });
      $("zoneName").value = "";
      setDrawing(false);
      load();
    } catch (e) { $("mapHint").textContent = "저장 실패: " + e.message; }
  });
  $("zoneList").addEventListener("click", async e => {
    const go = e.target.dataset.go, del = e.target.dataset.del;
    if (go && layers[go]) App.map.fitBounds(layers[go].getBounds(), { maxZoom: 20 });
    if (del) { await App.api(`/api/zones/${del}`, { method: "DELETE" }); load(); }
  });

  // 경보음 (브라우저 내장 오디오 — 파일 불필요)
  let actx;
  function beep() {
    if (!$("alertSound").checked) return;
    try {
      actx = actx || new AudioContext();
      [0, 0.25].forEach(dt => {
        const o = actx.createOscillator(), g = actx.createGain();
        o.frequency.value = 880; o.type = "square";
        g.gain.setValueAtTime(0.08, actx.currentTime + dt);
        g.gain.exponentialRampToValueAtTime(0.001, actx.currentTime + dt + 0.18);
        o.connect(g).connect(actx.destination);
        o.start(actx.currentTime + dt); o.stop(actx.currentTime + dt + 0.2);
      });
    } catch (_) { /* 오디오 불가 환경 */ }
  }
  function toast(a) {
    const t = document.createElement("div");
    t.className = "toast";
    t.innerHTML = `<b>⚠ 감시구역 경보 — ${esc(a.zone)}</b><br>${CLS_KO[a.cls]} ${a.obj_id} 진입 · ${a.time}`;
    t.addEventListener("click", () => { App.focusObject(a.obj_id); t.remove(); });
    $("toasts").prepend(t);
    setTimeout(() => t.remove(), 8000);
    const lay = layers[a.zone_id];
    if (lay) { lay.setStyle({ fillOpacity: 0.35 }); setTimeout(() => lay.setStyle({ fillOpacity: 0.08 }), 1500); }
  }

  App.on(s => {
    const alerts = s.alerts || [];
    const maxSeq = alerts.length ? alerts[alerts.length - 1].seq : 0;
    if (lastAlertSeq === null) lastAlertSeq = maxSeq;           // 페이지 열 때 과거 경보는 소리 없이
    const fresh = alerts.filter(a => a.seq > lastAlertSeq);
    if (fresh.length) { fresh.forEach(toast); beep(); }
    if (maxSeq < lastAlertSeq) lastAlertSeq = 0;                // 서버에서 초기화됨
    lastAlertSeq = Math.max(lastAlertSeq, maxSeq);
    if (alerts.length !== alertCount || fresh.length) {
      alertCount = alerts.length;
      $("alertTotal").textContent = alerts.length;
      $("alertList").innerHTML = alerts.length
        ? [...alerts].reverse().map(a => `<li data-id="${a.obj_id}"><span class="alert-time">${a.time}</span>
            <b>${esc(a.zone)}</b> · <span class="tag ${a.cls}">${CLS_KO[a.cls]}</span> ${a.obj_id}</li>`).join("")
        : `<li class="muted small">경보 없음</li>`;
    }
  });
  $("alertList").addEventListener("click", e => {
    const li = e.target.closest("[data-id]");
    if (li) App.focusObject(li.dataset.id);
  });

  App.ready.then(load);
})();

// ---------------------------------------------------------------- ① 정확도 검증
(() => {
  let acc = null;
  const truthLayer = L.layerGroup();
  let pickMarker = null;

  const tile = (label, value, hint) => `<div class="metric"><span>${label}</span><strong>${value}</strong>${hint ? `<em>${hint}</em>` : ""}</div>`;
  const m = v => (v == null ? "-" : `${v.toFixed(2)} m`);

  function render() {
    if (!acc) return;
    const e = acc.estimate, sm = acc.samples;
    $("accMeta").textContent = `기준점 ${acc.total}개 · 매칭 ${acc.matched}개 · 자동 매칭 반경 ${acc.match_radius} m`;
    $("accTiles").innerHTML =
      tile("평균 오차", m(e?.mean), "누적 추정 좌표 기준") +
      tile("CEP50", m(e?.cep50), "절반이 이 반경 안") +
      tile("CEP90", m(e?.cep90)) +
      tile("최대 오차", m(e?.max)) +
      tile("개별 탐지 RMSE", m(sm?.rmse), sm ? `표본 ${sm.n}개` : "") +
      tile("계통 편향 (북/동)", acc.bias ? `${acc.bias.dn.toFixed(1)} / ${acc.bias.de.toFixed(1)} m` : "-", "한쪽으로 치우치면 카메라 방향·화각 점검");

    const matched = acc.points.filter(p => p.obj_id);
    Charts.hbar($("accChart"), {
      unit: " m", ref: e?.mean,
      rows: matched.map(p => ({
        label: p.label, value: p.est_err,
        tip: `<b>${esc(p.label)}</b> ↔ ${p.obj_id}${p.manual ? " (수동)" : ""}<br>추정 오차 ${p.est_err.toFixed(2)} m<br>
              편차 북 ${p.dn.toFixed(1)} / 동 ${p.de.toFixed(1)} m<br>
              개별 탐지 평균 ${p.sample_stats.mean} m · CEP90 ${p.sample_stats.cep90} m (n=${p.sample_stats.n})`
      }))
    });

    $("accTable").innerHTML = acc.points.length ? acc.points.map(p => `
      <tr>
        <td><b>${esc(p.label)}</b>${p.cls ? ` <span class="tag ${p.cls}">${CLS_KO[p.cls]}</span>` : ""}<div class="small muted">${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}</div></td>
        <td>${p.obj_id ? `<button class="link" data-go="${p.obj_id}">${p.obj_id}</button>${p.manual ? " <span class='small muted'>수동</span>" : ""}` : '<span class="muted">미매칭</span>'}</td>
        <td>${p.est_err != null ? `<b>${p.est_err.toFixed(2)}</b>` : "-"}</td>
        <td>${p.dn != null ? `${p.dn.toFixed(1)} / ${p.de.toFixed(1)}` : "-"}</td>
        <td>${p.sample_stats ? p.sample_stats.mean.toFixed(2) : "-"}</td>
        <td>${p.sample_stats ? p.sample_stats.cep90.toFixed(2) : "-"}</td>
        <td>${p.sample_stats ? p.sample_stats.n : "-"}</td>
        <td><button class="link danger" data-del="${p.id}">삭제</button></td>
      </tr>`).join("")
      : `<tr><td colspan="8" class="muted">기준점을 추가하면 추정 좌표와의 오차가 계산됩니다.</td></tr>`;

    // 지도: 기준점 + 매칭 연결선
    truthLayer.clearLayers();
    for (const p of acc.points) {
      L.marker([p.lat, p.lon], {
        icon: L.divIcon({ className: "", html: `<div class="truth-icon"><span>T</span></div>`, iconSize: [22, 22], iconAnchor: [11, 11] }),
        title: p.label, zIndexOffset: -500
      }).bindTooltip(`${esc(p.label)}${p.est_err != null ? ` · 오차 ${p.est_err.toFixed(1)} m` : ""}`).addTo(truthLayer);
      const o = App.objects.find(x => x.id === p.obj_id);
      if (o) L.polyline([[p.lat, p.lon], [o.lat, o.lon]], { color: "#e5e7eb", weight: 1.5, dashArray: "3 4", interactive: false }).addTo(truthLayer);
    }
  }

  const refresh = () => App.api("/api/accuracy").then(a => { acc = a; render(); }).catch(() => {});

  function syncObjSelect() {
    const sel = $("tObj"), cur = sel.value;
    const ids = App.objects.map(o => o.id).sort();
    const sig = ids.join();
    if (sel.dataset.sig === sig) return;
    sel.dataset.sig = sig;
    sel.innerHTML = `<option value="">자동 (가장 가까운 객체)</option>` +
      App.objects.slice().sort((a, b) => a.id.localeCompare(b.id))
        .map(o => `<option value="${o.id}">${o.id} (${CLS_KO[o.cls]})</option>`).join("");
    sel.value = ids.includes(cur) ? cur : "";
  }

  function setPicking(on) {
    $("map").classList.toggle("picking", on);
    $("mapHint").hidden = !on;
    $("mapHint").textContent = "기준점 찍기: 표적의 실제 위치를 클릭";
    $("tPick").textContent = on ? "찍기 취소" : "지도에서 찍기";
    App.mapClick = on ? ll => {
      $("tLat").value = ll.lat.toFixed(7);
      $("tLon").value = ll.lng.toFixed(7);
      if (pickMarker) App.map.removeLayer(pickMarker);
      pickMarker = L.circleMarker(ll, { radius: 6, color: "#fff" }).addTo(App.map);
      setPicking(false);
    } : null;
  }
  $("tPick").addEventListener("click", () => setPicking(!App.mapClick));
  $("tDrone").addEventListener("click", async () => {
    const d = await App.api("/api/drone");
    if (d.lat == null) { $("tErr").textContent = "드론 GPS 위치가 없습니다"; return; }
    $("tLat").value = d.lat.toFixed(7);
    $("tLon").value = d.lon.toFixed(7);
    $("tErr").textContent = "";
  });
  $("truthForm").addEventListener("submit", async e => {
    e.preventDefault();
    const lat = parseFloat($("tLat").value), lon = parseFloat($("tLon").value);
    if (!(Math.abs(lat) <= 90 && Math.abs(lon) <= 180)) { $("tErr").textContent = "위도/경도를 입력하세요"; return; }
    try {
      await App.api("/api/truth", { method: "POST", body: JSON.stringify({
        label: $("tLabel").value.trim(), cls: $("tCls").value, obj_id: $("tObj").value, lat, lon }) });
      $("tErr").textContent = "";
      $("tLabel").value = $("tLat").value = $("tLon").value = "";
      $("tObj").value = "";
      if (pickMarker) { App.map.removeLayer(pickMarker); pickMarker = null; }
      refresh();
    } catch (err) { $("tErr").textContent = err.message; }
  });
  $("accTable").addEventListener("click", async e => {
    if (e.target.dataset.go) App.focusObject(e.target.dataset.go);
    if (e.target.dataset.del) { await App.api(`/api/truth/${e.target.dataset.del}`, { method: "DELETE" }); refresh(); }
  });
  $("showTruth").addEventListener("change", e => (e.target.checked ? truthLayer.addTo(App.map) : App.map.removeLayer(truthLayer)));
  $("accExport").addEventListener("click", () => {
    if (!acc) return;
    const head = "label,class,truth_lat,truth_lon,obj_id,est_err_m,dn_m,de_m,sample_mean_m,sample_cep50_m,sample_cep90_m,sample_max_m,n\n";
    const rows = acc.points.map(p => [p.label, p.cls || "", p.lat, p.lon, p.obj_id || "", p.est_err ?? "", p.dn ?? "", p.de ?? "",
      p.sample_stats?.mean ?? "", p.sample_stats?.cep50 ?? "", p.sample_stats?.cep90 ?? "", p.sample_stats?.max ?? "", p.sample_stats?.n ?? ""].join(","));
    const e = acc.estimate;
    const sum = e ? `\n\n# summary,matched=${acc.matched},mean=${e.mean},cep50=${e.cep50},cep90=${e.cep90},max=${e.max},rmse=${e.rmse}` : "";
    downloadText(`geo_accuracy_${stamp()}.csv`, head + rows.join("\n") + sum, "text/csv");
  });

  App.ready.then(() => { if ($("showTruth").checked) truthLayer.addTo(App.map); refresh(); });
  setInterval(refresh, 3000);
  App.on(syncObjSelect);
})();

// ---------------------------------------------------------------- ⑤ 보고서
(() => {
  let last = null;

  // 최소 마크다운 → HTML (제목, 목록, 표, 굵게) — 항상 이스케이프 후 변환
  function md(src) {
    const lines = src.split(/\r?\n/);
    let html = "", i = 0;
    const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
    while (i < lines.length) {
      const l = lines[i];
      if (/^\s*\|/.test(l)) {
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
        const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        const body = rows.filter(r => !/^\s*\|[\s:|-]+\|\s*$/.test(r));
        html += `<div class="table-wrap"><table><thead><tr>${cells(body[0]).map(c => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>` +
          body.slice(1).map(r => `<tr>${cells(r).map(c => `<td>${inline(c)}</td>`).join("")}</tr>`).join("") + `</tbody></table></div>`;
        continue;
      }
      if (/^\s*[-*] /.test(l)) {
        html += "<ul>";
        while (i < lines.length && /^\s*[-*] /.test(lines[i])) html += `<li>${inline(lines[i++].replace(/^\s*[-*] /, ""))}</li>`;
        html += "</ul>";
        continue;
      }
      const im = l.match(/^!\[([^\]]*)\]\((data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+)\)\s*$/);   // 보고서 그래프 (서버가 만든 SVG만 허용)
      if (im) { html += `<figure class="report-fig"><img src="${im[2]}" alt="${esc(im[1])}"></figure>`; i++; continue; }
      const h = l.match(/^(#{1,4})\s+(.*)/);
      if (h) html += `<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`;
      else if (l.trim()) html += `<p>${inline(l)}</p>`;
      i++;
    }
    return html;
  }

  // ---- 리플레이 보고서 기준 시점
  const mmss = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
  const vid = () => document.querySelector("video.demo-video");
  // 반환: 숫자(영상 초) · null(영상 끝까지) · undefined(입력 오류)
  function cutTime() {
    const m = $("rptCut").value, v = vid();
    if (m === "end") return null;
    if (m === "now") return v ? v.currentTime : null;
    const x = /^\s*(\d{1,3})\s*[:：분]\s*(\d{1,2})\s*초?\s*$/.exec($("rptCutAt").value) || /^\s*(\d+(?:\.\d+)?)\s*초?\s*$/.exec($("rptCutAt").value);
    if (!x) return undefined;
    const t = x[2] != null ? +x[1] * 60 + +x[2] : +x[1];
    const full = App.replay && App.replay.fullDuration ? App.replay.fullDuration() : Infinity;
    return Math.min(t, full);
  }
  function syncCutUi() {
    const box = $("rptCutBox");
    box.hidden = !App.replay;
    if (!App.replay) return;
    const v = vid(), full = App.replay.fullDuration ? App.replay.fullDuration() : 0;
    const o = $("rptCut").querySelector('option[value="now"]');
    o.textContent = `현재 재생 위치 (${v ? mmss(v.currentTime) : "-"})`;
    $("rptCut").querySelector('option[value="end"]').textContent = `영상 끝까지 (${mmss(full)})`;
    $("rptCutAt").hidden = $("rptCut").value !== "at";
  }
  $("rptCut").addEventListener("change", () => { syncCutUi(); if ($("rptCut").value === "at") { const v = vid(); if (!$("rptCutAt").value && v) $("rptCutAt").value = mmss(v.currentTime); $("rptCutAt").focus(); } });
  setInterval(syncCutUi, 500);
  App.ready.then(syncCutUi);

  $("reportBtn").addEventListener("click", async () => {
    const btn = $("reportBtn");
    btn.disabled = true;
    btn.textContent = "작성 중…";
    $("reportBody").innerHTML = `<p class="muted">임무 데이터를 모아 보고서를 작성하고 있습니다… (AI 사용 시 10~30초)</p>`;
    try {
      // 리플레이 중이면 '기준 시점'까지의 결과로 보고서 작성 (현재 재생 위치 / 직접 입력 / 영상 끝)
      const tCut = App.replay ? cutTime() : null;
      if (App.replay && tCut === undefined) throw new Error("기준 시점을 분:초 형식으로 입력하세요 (예: 3:15)");
      const rp = App.replay && App.replay.reportData ? App.replay.reportData(tCut) : null;
      const series = App.countSeries ? App.countSeries(tCut) : null;   // 객체 수 그래프 데이터 (count-chart.js)
      last = await App.api("/api/report", { method: "POST", body: JSON.stringify(Object.assign(rp ? { replay: rp } : {}, series ? { series } : {},
        App.eventLog ? { events: App.eventLog(tCut).slice(-600) } : {}, App.coverageReport ? { coverage: App.coverageReport(tCut) } : {})) });
      $("reportBody").innerHTML = md(last.report);
      const b = $("reportSource");
      b.hidden = false;
      b.textContent = last.source === "claude" ? "Claude AI 작성" : "규칙 기반";
      b.className = "badge " + (last.source === "claude" ? "ai" : "");
      $("reportNote").textContent = last.note || `서버에도 저장됨: data/reports/${last.filename}`;
      $("reportCopy").disabled = $("reportSave").disabled = false;
    } catch (e) {
      $("reportBody").innerHTML = `<p class="err">보고서 생성 실패: ${esc(e.message)}</p>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "보고서 생성";
    }
  });
  $("reportCopy").addEventListener("click", () => last && navigator.clipboard?.writeText(last.report)
    .then(() => { $("reportCopy").textContent = "복사됨"; setTimeout(() => ($("reportCopy").textContent = "복사"), 1500); }));
  $("reportSave").addEventListener("click", () => last && downloadText(last.filename, last.report, "text/markdown"));
  App.ready.then(cfg => {
    if (!cfg.ai) $("reportNote").textContent += " (현재 API 키가 없어 규칙 기반으로 작성됩니다)";
  });
})();
