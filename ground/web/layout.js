// 영상 | 지도 칸 너비 조절 — 가운데 손잡이를 드래그, 더블클릭하면 50:50
(function () {
  const dash = document.querySelector(".dashboard");
  const video = document.querySelector(".video-card");
  if (!dash || !video) return;
  const KEY = "dash.split";
  const MIN = 20, MAX = 80;

  function apply(p) {
    p = Math.max(MIN, Math.min(MAX, p));
    dash.style.setProperty("--split-l", p + "fr");
    dash.style.setProperty("--split-r", (100 - p) + "fr");
    return p;
  }
  let cur = 50;
  try { const v = parseFloat(localStorage.getItem(KEY)); if (v) cur = v; } catch (e) {}
  cur = apply(cur);

  const h = document.createElement("div");
  h.className = "split-handle";
  h.title = "드래그해서 영상/지도 크기 조절 · 더블클릭하면 반반";
  video.appendChild(h);

  function save() { try { localStorage.setItem(KEY, String(cur)); } catch (e) {} }

  h.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    h.setPointerCapture(e.pointerId);
    document.body.classList.add("splitting");
    const r = dash.getBoundingClientRect();
    const cs = getComputedStyle(dash);
    const left = r.left + parseFloat(cs.paddingLeft);
    const width = r.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    const move = (ev) => { cur = apply(((ev.clientX - left) / width) * 100); };
    const up = () => {
      h.removeEventListener("pointermove", move);
      h.removeEventListener("pointerup", up);
      document.body.classList.remove("splitting");
      save();
    };
    h.addEventListener("pointermove", move);
    h.addEventListener("pointerup", up);
  });
  h.addEventListener("dblclick", () => { cur = apply(50); save(); });
})();

// 비행 상태 / 성능 모니터 카드 높이 조절 — 아래 가장자리 드래그, 더블클릭하면 원래 크기
(function () {
  [["flight-card", "dash.h.flight"], ["perf-card", "dash.h.perf"]].forEach(([cls, key]) => {
    const card = document.querySelector("." + cls);
    if (!card) return;
    const MIN = 160;
    function setH(px) {
      if (px == null) { card.style.height = ""; card.classList.remove("sized"); return; }
      card.style.height = Math.max(MIN, Math.round(px)) + "px";
      card.classList.add("sized");
    }
    try { const v = parseFloat(localStorage.getItem(key)); if (v) setH(v); } catch (e) {}

    const h = document.createElement("div");
    h.className = "vsplit-handle";
    h.title = "드래그해서 높이 조절 · 더블클릭하면 원래 크기";
    card.appendChild(h);

    h.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      h.setPointerCapture(e.pointerId);
      document.body.classList.add("vsplitting");
      const y0 = e.clientY, h0 = card.getBoundingClientRect().height;
      const move = (ev) => setH(h0 + ev.clientY - y0);
      const up = () => {
        h.removeEventListener("pointermove", move);
        h.removeEventListener("pointerup", up);
        document.body.classList.remove("vsplitting");
        try { localStorage.setItem(key, String(card.getBoundingClientRect().height)); } catch (e) {}
      };
      h.addEventListener("pointermove", move);
      h.addEventListener("pointerup", up);
    });
    h.addEventListener("dblclick", () => {
      setH(null);
      try { localStorage.removeItem(key); } catch (e) {}
    });
  });
})();
