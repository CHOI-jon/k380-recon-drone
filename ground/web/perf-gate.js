// 시스템 성능 모니터는 라즈베리파이 드론(K380)이 실제로 연결돼 있을 때만 값을 보여 준다.
// 리플레이(매빅·결과 영상·K380 녹화) 중이거나 시뮬레이션이면 안내 문구로 가린다.
(function () {
  if (typeof App === "undefined" || !App.ready) return;
  const card = document.querySelector(".perf-card");
  if (!card) return;
  const box = document.createElement("div");
  box.className = "perf-off";
  box.hidden = true;
  card.appendChild(box);

  function update() {
    const live = !!(App.cfg && App.cfg.video_url);
    const rp = App.replay;
    let msg = "";
    if (rp) {
      const kind = (rp.data && rp.data.kind) || "";
      const hasPerf = !!(rp.data && rp.data.perf && rp.data.perf.length);
      msg = kind === "k380"
        ? (hasPerf ? "" : "K380 녹화 리플레이 중<br><span>이 녹화에는 파이 성능 기록이 없습니다 (detector v3.4 이후 녹화부터 표시)</span>")
        : "매빅 4 Pro 영상 리플레이 중<br><span>시스템 성능 모니터는 라즈베리파이 드론(K380)이 연결됐을 때만 작동합니다</span>";
    } else if (!live) {
      msg = "드론 미연결 (시뮬레이션)<br><span>라즈베리파이 드론(K380)이 연결되면 실제 값이 표시됩니다</span>";
    }
    box.innerHTML = msg;
    box.hidden = !msg;
    card.classList.toggle("perf-gated", !!msg);
  }
  App.ready.then(update);
  App.on(update);
  document.addEventListener("replay-change", update);
})();
