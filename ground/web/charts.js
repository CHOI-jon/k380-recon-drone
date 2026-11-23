// 작은 SVG 차트 모음 — 외부 라이브러리 없음
// 원칙: 단일 계열(범례 없음, 제목이 이름), 얇은 선, 흐린 격자, 호버 툴팁
const Charts = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const tip = () => document.getElementById("tooltip");
  const el = (tag, attrs = {}) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };
  function showTip(evt, html) {
    const t = tip();
    t.innerHTML = html;
    t.hidden = false;
    const pad = 14, w = t.offsetWidth, h = t.offsetHeight;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
    if (y + h > innerHeight - 8) y = evt.clientY - h - pad;
    t.style.left = x + "px";
    t.style.top = y + "px";
  }
  const hideTip = () => (tip().hidden = true);
  const fmtTime = t => new Date(t * 1000).toLocaleTimeString("ko-KR", { hour12: false });

  // 스파크라인: data = [{t, v}], v가 null이면 선을 끊는다
  function spark(container, { title, unit = "", digits = 0, data, yMin, yMax, warn }) {
    // 성능 카드 높이를 사용자가 조절한 경우(.sized) 실제 칸 크기에 맞춰 그림
    let W = 260, H = 64;
    if (container.closest && container.closest(".perf-card.sized")) {
      W = Math.max(120, container.clientWidth - 20);
      H = Math.max(40, container.clientHeight - 34);
    }
    const P = { l: 30, r: 6, t: 6, b: 14 };
    const vals = data.map(d => d.v).filter(v => v != null);
    const last = [...data].reverse().find(d => d.v != null);
    let lo = yMin ?? (vals.length ? Math.min(...vals) : 0);
    let hi = yMax ?? (vals.length ? Math.max(...vals) : 1);
    if (yMin == null && vals.length) lo = Math.min(lo, Math.max(0, lo - (hi - lo) * 0.2));
    if (hi - lo < 1e-6) hi = lo + 1;
    if (yMax == null) hi = hi + (hi - lo) * 0.1;
    const t0 = data.length ? data[0].t : 0, t1 = data.length ? data[data.length - 1].t : 1;
    const x = t => P.l + ((t - t0) / Math.max(1, t1 - t0)) * (W - P.l - P.r);
    const y = v => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);

    const warnOn = warn && last && warn(last.v);
    container.innerHTML = `<div class="spark-head"><span>${title}</span>
      <strong class="${warnOn ? "warn-text" : ""}">${last ? last.v.toFixed(digits) : "-"}<small>${unit}</small></strong></div>`;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark-svg", role: "img",
      "aria-label": `${title} 최근 값 ${last ? last.v.toFixed(digits) + unit : "없음"}` });
    // 격자 + 축 (흐리게)
    [lo, (lo + hi) / 2, hi].forEach(v => {
      svg.appendChild(el("line", { x1: P.l, x2: W - P.r, y1: y(v), y2: y(v), class: "grid" }));
      const tx = el("text", { x: P.l - 4, y: y(v) + 3, class: "axis", "text-anchor": "end" });
      tx.textContent = v.toFixed(v >= 100 ? 0 : digits);
      svg.appendChild(tx);
    });
    let dstr = "", pen = false;
    for (const d of data) {
      if (d.v == null) { pen = false; continue; }
      dstr += `${pen ? "L" : "M"}${x(d.t).toFixed(1)},${y(d.v).toFixed(1)}`;
      pen = true;
    }
    svg.appendChild(el("path", { d: dstr, class: "spark-line" }));
    if (last) svg.appendChild(el("circle", { cx: x(last.t), cy: y(last.v), r: 3, class: "spark-dot" }));

    // 호버: 십자선 + 툴팁
    const cross = el("line", { y1: P.t, y2: H - P.b, class: "cross", visibility: "hidden" });
    const hdot = el("circle", { r: 4, class: "spark-dot hover", visibility: "hidden" });
    svg.appendChild(cross);
    svg.appendChild(hdot);
    const hit = el("rect", { x: P.l, y: 0, width: W - P.l - P.r, height: H, fill: "transparent" });
    hit.addEventListener("mousemove", evt => {
      if (!data.length) return;
      const r = svg.getBoundingClientRect();
      const px = ((evt.clientX - r.left) / r.width) * W;
      const tt = t0 + ((px - P.l) / (W - P.l - P.r)) * (t1 - t0);
      let best = null;
      for (const d of data) if (d.v != null && (!best || Math.abs(d.t - tt) < Math.abs(best.t - tt))) best = d;
      if (!best) return;
      cross.setAttribute("x1", x(best.t)); cross.setAttribute("x2", x(best.t));
      hdot.setAttribute("cx", x(best.t)); hdot.setAttribute("cy", y(best.v));
      cross.setAttribute("visibility", "visible"); hdot.setAttribute("visibility", "visible");
      showTip(evt, `<b>${title}</b><br>${fmtTime(best.t)} · ${best.v.toFixed(digits)}${unit}`);
    });
    hit.addEventListener("mouseleave", () => {
      cross.setAttribute("visibility", "hidden"); hdot.setAttribute("visibility", "hidden"); hideTip();
    });
    svg.appendChild(hit);
    container.appendChild(svg);
  }

  // 가로 막대: rows = [{label, value, tip}] — 기준 0에서 시작, 끝에 값 라벨
  function hbar(container, { rows, unit = "", digits = 1, ref }) {
    if (!rows.length) { container.innerHTML = `<p class="muted small">매칭된 기준점이 없습니다.</p>`; return; }
    const rowH = 26, W = Math.max(320, Math.round(container.clientWidth - 28) || 520), P = { l: 110, r: 56, t: 8, b: 20 };
    const H = P.t + P.b + rows.length * rowH;
    const max = Math.max(...rows.map(r => r.value), ref || 0) * 1.05 || 1;
    const x = v => P.l + (v / max) * (W - P.l - P.r);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "hbar-svg", role: "img", "aria-label": "기준점별 위치추정 오차" });
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const v = (max / ticks) * i;
      svg.appendChild(el("line", { x1: x(v), x2: x(v), y1: P.t, y2: H - P.b, class: "grid" }));
      const tx = el("text", { x: x(v), y: H - 6, class: "axis", "text-anchor": "middle" });
      tx.textContent = v.toFixed(v >= 10 ? 0 : 1);
      svg.appendChild(tx);
    }
    rows.forEach((r, i) => {
      const cy = P.t + i * rowH + rowH / 2;
      const lb = el("text", { x: P.l - 8, y: cy + 4, class: "label", "text-anchor": "end" });
      lb.textContent = r.label.length > 12 ? r.label.slice(0, 11) + "…" : r.label;
      svg.appendChild(lb);
      const w = Math.max(2, x(r.value) - P.l);
      // 기준선에 붙은 쪽은 각지게, 끝은 4px 라운드
      const h = 14, y0 = cy - h / 2, rr = Math.min(4, w);
      svg.appendChild(el("path", { class: "bar",
        d: `M${P.l},${y0}H${P.l + w - rr}Q${P.l + w},${y0} ${P.l + w},${y0 + rr}V${y0 + h - rr}Q${P.l + w},${y0 + h} ${P.l + w - rr},${y0 + h}H${P.l}Z` }));
      const vt = el("text", { x: P.l + w + 6, y: cy + 4, class: "value" });
      vt.textContent = `${r.value.toFixed(digits)}${unit}`;
      svg.appendChild(vt);
      const hit = el("rect", { x: 0, y: cy - rowH / 2, width: W, height: rowH, fill: "transparent" });
      hit.addEventListener("mousemove", evt => showTip(evt, r.tip));
      hit.addEventListener("mouseleave", hideTip);
      svg.appendChild(hit);
    });
    if (ref) {
      svg.appendChild(el("line", { x1: x(ref), x2: x(ref), y1: P.t - 4, y2: H - P.b, class: "ref" }));
      const t = el("text", { x: x(ref) + 4, y: P.t + 4, class: "axis" });
      t.textContent = `평균 ${ref.toFixed(1)}${unit}`;
      svg.appendChild(t);
    }
    container.innerHTML = "";
    container.appendChild(svg);
  }

  return { spark, hbar, showTip, hideTip };
})();
