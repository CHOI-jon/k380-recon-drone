// 시뮬레이션 모드(파이 영상 없음)일 때 web/demo.mp4 가 있으면 영상 칸에 반복 재생
(function () {
  if (typeof App === "undefined" || !App.ready) return;
  App.ready.then((cfg) => {
    if (cfg && cfg.video_url) return;              // 실제 파이 영상이 있으면 그대로 사용
    const frame = document.querySelector(".video-frame");
    const empty = document.getElementById("videoEmpty");
    const img = document.getElementById("droneVideo");
    const v = document.createElement("video");
    v.className = "demo-video";
    v.muted = true; v.loop = true; v.autoplay = true; v.playsInline = true; v.controls = true;
    v.src = "demo.mp4?v=ft1";
    v.addEventListener("loadeddata", () => {
      empty.hidden = true;
      if (img) img.style.display = "none";
      frame.appendChild(v);
      v.play().catch(() => {});
    });
    v.addEventListener("error", () => {});        // 파일 없으면 기존 "영상 없음" 문구 유지
    v.load();
  });
})();
