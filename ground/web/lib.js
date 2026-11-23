// 자료 보관함 — 녹화 영상(K380·매빅)과 비행 로그(.bin)를 넣어 두고 대시보드에서 바로 재생·분석
(function () {
  if (typeof App === "undefined" || !App.ready) return;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const panel = $("libPanel"), listEl = $("libList");
  let data = null, timer = 0, openSet = new Set();

  const fmtSize = (b) => (b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB");
  const fmtDur = (s) => (s == null ? "" : `${Math.floor(s / 60)}분 ${Math.round(s % 60)}초`);
  const fmtDate = (iso) => (iso ? iso.replace("T", " ").slice(0, 16) : "");

  function open() { panel.hidden = false; document.body.classList.add("lib-open"); refresh(); }
  function close() { panel.hidden = true; document.body.classList.remove("lib-open"); clearTimeout(timer); }
  $("libBtn").addEventListener("click", open);
  $("libClose").addEventListener("click", close);
  panel.addEventListener("click", (e) => { if (e.target === panel) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) close(); });

  async function refresh() {
    clearTimeout(timer);
    try {
      data = await App.api("/api/lib");
      render();
    } catch (e) {
      listEl.innerHTML = `<p class="err">자료 목록을 불러오지 못했습니다: ${esc(e.message)}</p>`;
    }
    const busy = data && data.items.some((m) => ["queued", "processing"].includes(m.status.state));
    const installing = data && data.install && data.install.state === "running";
    if (!panel.hidden) timer = setTimeout(refresh, busy || installing ? 1000 : 4000);
  }

  function statusHtml(m) {
    const s = m.status;
    if (s.state === "processing" || s.state === "queued") {
      const pct = Math.round((s.progress || 0) * 100);
      return `<div class="lib-prog"><div style="width:${pct}%"></div></div><span class="small">${esc(s.msg || "")}</span>`;
    }
    if (s.state === "error") return `<span class="lib-st err">오류 — ${esc(s.msg)}</span>`;
    if (s.state === "ready") return `<span class="lib-st ok">준비됨</span>`;
    return `<span class="lib-st">${esc(s.msg || "분석 전")}</span>`;
  }

  function resultHtml(m) {
    const r = m.result || {};
    if (m.kind === "log") {
      if (!r.length && r.length !== 0) return "";
      return `비행 구간 ${r.segments ?? "-"}개 · 기록 ${fmtDur(r.length)}${r.fft_peak ? ` · 진동 주파수 ${Math.round(r.fft_peak)} Hz` : ""}`;
    }
    if (m.kind === "demo") return "운동장·관중석 · 사람 73 / 차량 31 (640 분석)";
    if (m.kind === "result") return `사람 <b>${r.persons}</b> · 차량 <b>${r.vehicles}</b> · ${esc(r.model || "")}${r.note ? ` · <span class="muted">${esc(r.note)}</span>` : ""}`;
    if (r.detections == null) return "";
    return `사람 <b>${r.persons}</b> · 차량 <b>${r.vehicles}</b> · 탐지 ${r.detections}회 · ${fmtDur(r.duration)} · ${esc(r.video || "")} · ${esc(r.model || "")} · 분석 ${fmtDur(r.took)}`;
  }

  function settingsHtml(m) {
    const p = m.params || {};
    const models = (data.models || []).map((n) => `<option ${n === p.model ? "selected" : ""}>${esc(n)}</option>`).join("");
    if (m.kind === "log") return "";
    const hf = p.hfov ?? (m.kind === "mavic" ? 24.2 : 66);
    return `<div class="lib-set" data-set="${esc(m.id)}">
      <label>모델<select name="model">${models || "<option value=''>models 폴더에 모델 없음</option>"}</select></label>
      <label>입력 크기<select name="imgsz"><option ${p.imgsz == 416 ? "selected" : ""}>416</option><option ${p.imgsz != 416 ? "selected" : ""}>640</option></select></label>
      <label>신뢰도 기준<input name="conf" type="number" step="0.05" min="0.1" max="0.9" value="${p.conf ?? 0.35}"></label>
      <label>초당 분석<select name="fps">${[2, 4, 8].map((f) => `<option ${f == (p.fps ?? 4) ? "selected" : ""}>${f}</option>`).join("")}</select></label>
      <label>카메라 수평 화각(°)<input name="hfov" type="number" step="0.1" value="${hf}"></label>
      <button class="lib-run" data-id="${esc(m.id)}">이 설정으로 분석</button>
      <p class="muted small">${m.kind === "mavic" ? "매빅 화각: 0314 실측 24.2° (3배 망원 계열) · 1배 광각이면 약 65°" : "K380 파이 카메라(Camera Module 3 표준) 약 66°"}</p>
    </div>`;
  }

  function render() {
    const cur = App.replayCtl && App.replayCtl.current();
    const live = App.cfg && App.cfg.video_url;
    $("libNow").innerHTML = cur
      ? `지금 보는 중: <b>${esc(cur.name)}</b> <button class="ghost" id="libBack">${live ? "실시간 화면으로" : "재생 끄기"}</button>`
      : live ? "지금: <b>실시간 화면</b> (드론 연결)" : "지금: 시뮬레이션 (재생 중인 자료 없음)";
    const bb = $("libBack");
    if (bb) bb.onclick = () => { App.replayCtl.stop(); render(); };

    const dp = data.deps || {};
    const ins = data.install || {};
    $("libDeps").hidden = dp.ultralytics && dp.cv2;
    $("libDeps").innerHTML = (dp.ultralytics && dp.cv2) ? "" :
      `영상 분석에 필요한 프로그램이 없습니다 (${!dp.ultralytics ? "ultralytics " : ""}${!dp.cv2 ? "opencv" : ""}). 재생과 로그 분석은 됩니다.
       <button id="libInstall" ${ins.state === "running" ? "disabled" : ""}>${ins.state === "running" ? "설치 중…" : "필요 프로그램 설치"}</button>
       ${ins.state === "done" ? "<b>설치 완료 — 실행기에서 [정지] 후 [시작]으로 다시 켜세요</b>" : ""}
       ${ins.log ? `<pre class="lib-log">${esc(ins.log.split("\n").slice(-6).join("\n"))}</pre>` : ""}`;
    const ib = $("libInstall");
    if (ib) ib.onclick = () => App.api("/api/lib/install", { method: "POST" }).then(refresh);
    $("libFolder").textContent = data.folder;

    const pend = data.pending || [];
    $("libPending").hidden = !pend.length;
    $("libPending").innerHTML = pend.length ? `<b>정리되지 않은 파일</b><ul>${pend.map((p) => `<li>${esc(p.name)} — ${esc(p.reason)}</li>`).join("")}</ul>` : "";

    if (!data.items.length) {
      listEl.innerHTML = `<p class="muted">아직 넣어 둔 자료가 없습니다. 위에 파일을 끌어다 놓으세요.</p>`;
      return;
    }
    listEl.innerHTML = data.items.map((m) => {
      const playing = cur && cur.id === m.id;
      const busy = ["queued", "processing"].includes(m.status.state);
      const canPlay = m.kind !== "log" && m.has_track;
      const btns = [
        canPlay ? `<button class="lib-play" data-id="${esc(m.id)}" ${playing ? "disabled" : ""}>${playing ? "재생 중" : "▶ 재생"}</button>` : "",
        m.kind === "log" && m.has_log ? `<a class="btn" href="log.html?id=${encodeURIComponent(m.id)}" target="_blank">분석 보기</a>` : "",
        busy ? `<button class="ghost lib-cancel" data-id="${esc(m.id)}">취소</button>` : "",
        !busy && m.kind === "log" ? `<button class="ghost lib-redo" data-id="${esc(m.id)}">다시 분석</button>` : "",
        !busy && (m.kind === "k380" || m.kind === "mavic") ? `<button class="ghost lib-toggle" data-id="${esc(m.id)}">${m.has_track ? "다시 분석 ⚙" : "분석 설정 ⚙"}</button>` : "",
      ].join("");
      return `<div class="lib-item${playing ? " playing" : ""}">
        <div class="lib-head">
          ${m.rank ? `<span class="lib-rank">#${m.rank}</span>` : ""}
          <span class="lib-kind k-${m.kind}">${esc(m.kind_ko)}</span>
          <b class="lib-name">${esc(m.name)}</b>
          <span class="lib-dates small">${m.start ? `촬영 <b>${fmtDate(m.start)}</b>` : ""}${m.created ? ` · 추가 ${fmtDate(m.created)}` : ""}${m.size ? " · " + fmtSize(m.size) : ""}</span>
          <span class="lib-btns">${btns}</span>
        </div>
        <div class="lib-sub">${statusHtml(m)} <span class="small">${resultHtml(m)}</span></div>
        ${openSet.has(m.id) && !busy ? settingsHtml(m) : ""}
      </div>`;
    }).join("");
  }

  listEl.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const id = b.dataset.id;
    const m = data.items.find((x) => x.id === id);
    if (b.classList.contains("lib-play")) {
      b.disabled = true; b.textContent = "불러오는 중…";
      try { await App.replayCtl.load({ id: m.id, name: m.name, v: (m.status.done || "").replace(/\D/g, "") }); close(); }
      catch (err) { alert(err.message); render(); }
    } else if (b.classList.contains("lib-toggle")) {
      openSet.has(id) ? openSet.delete(id) : openSet.add(id);
      render();
    } else if (b.classList.contains("lib-run")) {
      const box = b.closest(".lib-set");
      const p = {};
      box.querySelectorAll("[name]").forEach((el) => (p[el.name] = el.value));
      openSet.delete(id);
      await App.api(`/api/lib/${encodeURIComponent(id)}/process`, { method: "POST", body: JSON.stringify(p) }).catch((err) => alert(err.message));
      refresh();
    } else if (b.classList.contains("lib-redo")) {
      await App.api(`/api/lib/${encodeURIComponent(id)}/process`, { method: "POST", body: "{}" }).catch((err) => alert(err.message));
      refresh();
    } else if (b.classList.contains("lib-cancel")) {
      await App.api(`/api/lib/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      refresh();
    }
  });

  // ---- 올리기 (끌어다 놓기 / 파일 선택)
  const drop = $("libDrop"), input = $("libFile");
  $("libPick").addEventListener("click", () => input.click());
  input.addEventListener("change", () => { if (input.files.length) upload(input.files); input.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) upload(e.dataTransfer.files); });
  // 패널 밖 아무 데나 떨어뜨려도 브라우저가 파일을 열지 않게
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => { e.preventDefault(); if (panel.hidden && e.dataTransfer.files.length) { open(); upload(e.dataTransfer.files); } });

  function upload(files) {
    const fd = new FormData();
    let total = 0;
    for (const f of files) { fd.append("file", f, f.name); total += f.size; }
    const bar = $("libUp");
    bar.hidden = false;
    bar.innerHTML = `<div class="lib-prog"><div style="width:0%"></div></div><span class="small">올리는 중 0% (${fmtSize(total)})</span>`;
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/lib/upload");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const p = Math.round((e.loaded / e.total) * 100);
      bar.querySelector(".lib-prog div").style.width = p + "%";
      bar.querySelector("span").textContent = `올리는 중 ${p}% (${fmtSize(total)})`;
    };
    xhr.onload = () => {
      bar.hidden = true;
      try {
        const j = JSON.parse(xhr.responseText);
        if (xhr.status >= 400) throw new Error(j.error || xhr.status);
      } catch (err) { alert("올리기 실패: " + err.message); }
      refresh();
    };
    xhr.onerror = () => { bar.hidden = true; alert("올리기 실패 (연결 끊김)"); };
    xhr.send(fd);
  }

  $("libOpenFolder").addEventListener("click", () => App.api("/api/lib/open-folder", { method: "POST" }).catch((e) => alert(e.message)));
  document.addEventListener("replay-change", () => { if (!panel.hidden && data) render(); });
})();
