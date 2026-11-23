/* recorder.js — 대시보드 탭 녹화 (app.js 수정 불필요)
 * 사용법: index.html 맨 아래에 <script src="recorder.js"></script> 추가
 * - 화면 우측 하단에 ● REC 버튼 생성
 * - 클릭 → 공유 팝업에서 "이 탭" 선택 → 녹화 시작
 * - 다시 클릭(또는 브라우저의 '공유 중지') → 파일 자동 다운로드
 * 요구사항: Chrome/Edge, localhost 또는 HTTPS 접속
 */
(function () {
  let recorder = null, chunks = [], stream = null, startTime = 0, timerId = null;

  // ---- 버튼 UI ----
  const btn = document.createElement('button');
  btn.id = 'rec-btn';
  btn.textContent = '● REC';
  Object.assign(btn.style, {
    position: 'fixed', right: '16px', bottom: '16px', zIndex: 99999,
    padding: '8px 14px', border: 'none', borderRadius: '6px',
    background: '#333', color: '#fff', font: 'bold 14px sans-serif',
    cursor: 'pointer', boxShadow: '0 2px 6px rgba(0,0,0,.4)'
  });
  document.body.appendChild(btn);

  // ---- 지원 포맷 선택 (MP4 우선, 안 되면 WebM) ----
  function pickMime() {
    const list = [
      'video/mp4;codecs=avc1',
      'video/mp4',
      'video/webm;codecs=vp9',
      'video/webm;codecs=vp8',
      'video/webm'
    ];
    return list.find(t => MediaRecorder.isTypeSupported(t)) || '';
  }

  function stamp() {
    const d = new Date(), p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_` +
           `${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  }

  function tick() {
    const s = Math.floor((Date.now() - startTime) / 1000);
    btn.textContent = `■ STOP ${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }

  async function start() {
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { displaySurface: 'browser', frameRate: 30 },
        audio: false,
        preferCurrentTab: true,      // Chrome: 현재 탭을 기본 선택
        selfBrowserSurface: 'include'
      });
    } catch (e) {
      console.warn('녹화 취소됨:', e);
      return;
    }

    // (선택) 대시보드 특정 영역만 잘라서 녹화 — Chrome Region Capture
    // 예: const target = document.querySelector('#main-panel');
    // if (target && window.CropTarget) {
    //   const crop = await CropTarget.fromElement(target);
    //   await stream.getVideoTracks()[0].cropTo(crop);
    // }

    const mimeType = pickMime();
    recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 6_000_000 });
    chunks = [];
    recorder.ondataavailable = e => e.data.size && chunks.push(e.data);
    recorder.onstop = save;

    // 사용자가 브라우저 '공유 중지'를 눌러도 정상 저장
    stream.getVideoTracks()[0].addEventListener('ended', stop);

    recorder.start(1000);           // 1초 단위로 청크 저장 (중간에 끊겨도 손실 최소화)
    startTime = Date.now();
    timerId = setInterval(tick, 500);
    btn.style.background = '#c62828';
    tick();
  }

  function stop() {
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    if (stream) stream.getTracks().forEach(t => t.stop());
    clearInterval(timerId);
    btn.style.background = '#333';
    btn.textContent = '● REC';
  }

  function save() {
    const type = recorder.mimeType || 'video/webm';
    const ext = type.includes('mp4') ? 'mp4' : 'webm';
    const blob = new Blob(chunks, { type });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `dashboard_${stamp()}.${ext}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    recorder = null; stream = null; chunks = [];
  }

  btn.addEventListener('click', () => (recorder ? stop() : start()));
})();
