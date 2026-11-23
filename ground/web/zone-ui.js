// 감시구역 패널: 지도 위 [▱ 감시구역] 버튼을 누를 때만 지도 오른쪽에 떠서 열린다
(function () {
  if (typeof App === "undefined" || !App.ready) return;
  const panel = document.querySelector(".map-card .zone-panel");
  const btn = document.getElementById("zoneToggle");
  if (!panel || !btn) return;
  const head = panel.querySelector("h3");
  const x = document.createElement("button");
  x.className = "link zone-close"; x.type = "button"; x.textContent = "닫기 ✕";
  if (head) head.appendChild(x);

  const set = (on) => { panel.classList.toggle("open", on); btn.classList.toggle("on", on); };
  btn.addEventListener("click", () => set(!panel.classList.contains("open")));
  x.addEventListener("click", () => {
    const drawing = !document.getElementById("zoneDrawing").hidden;
    if (drawing) document.getElementById("zoneCancel").click();
    set(false);
  });
  // 버튼에 구역 수·경보 수 표시
  App.on((s) => {
    const nz = (App.zones || []).length, na = (s.alerts || []).length;
    btn.innerHTML = `▱ 감시구역${nz ? ` <b>${nz}</b>` : ""}${na ? ` <span class="zone-alerts">⚠ ${na}</span>` : ""}`;
  });
})();
