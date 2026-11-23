#!/usr/bin/env python3
"""
detector_node.py v3.4 — 라즈베리파이 탐지 노드

v3.4: 녹화 csv에 파이 성능(추론 FPS·추론 시간·온도·CPU·스로틀)과 카메라(노출·게인·선명도)를 같이 기록
      → 대시보드 자료 보관함에서 K380 녹화를 재생할 때 시스템 성능 모니터에 표시

v3.3: 영상 흐림 대책 두 가지
      ① 짧은 노출(--shutter-us): 셔터 시간을 고정하고 밝기는 게인으로 보충 → 진동 중 번짐 감소
         (예: 2000 = 1/500초. 밝은 낮 비행용. 어두우면 노이즈가 늘어남)
      ② 선명한 프레임 골라 추론(--sharp-window): 최근 N장 중 가장 선명한(라플라시안 분산 최대)
         프레임을 추론에 넣는다. 영상 송출·녹화는 그대로 최신 프레임. 1이면 끔
      10초마다 [cam] 로그에 실제 노출·게인·선명도·흐린 프레임 비율을 출력
      재시작·종료(SIGTERM) 때도 녹화 중인 mp4를 정상적으로 닫음

v3.2: 원본 영상 녹화(--record) — 박스 없는 카메라 원본을 파이 SD카드에 mp4로 저장하고,
      같은 시각의 위치·고도·자세를 옆에 csv로 남긴다 (추가 학습용 데이터 / 비행 후 재분석용).
      파이 4의 하드웨어 H.264 인코더를 써서 탐지 속도에 거의 영향이 없다.

v3.1: VisDrone 등 학습 모델의 클래스를 metadata.yaml에서 자동 인식 / FC 하트비트만 사용
      (모드 깜빡임 수정) / 위성 수·ARM 상태 전송 / 초점 무한대 고정(--lens-pos)

v2 대비 변경점 두 가지:

 ① MAVLink 자세 직수신 (--mavlink)
    파이가 FC에서 ATTITUDE를 20Hz로 직접 받아 모노토닉 시각과 함께 링버퍼에 보관하고,
    프레임 캡처 시각 t_cap에 맞춰 선형 보간한 자세를 탐지 결과에 붙여 보낸다.
    → 노트북이 age_ms로 과거 자세를 되짚을 필요가 없어지고, 433MHz + 네트워크 지연에서
      오던 동기화 오차가 사라진다. (v1 문서 "알려진 한계"의 해소 경로)

 ② 추론 백엔드 raw ncnn (--backend)
    ultralytics는 torch를 하드 의존성으로 끌고 와 Pi 4에서 수 GB / 수십 분이 걸린다.
    런타임에는 불필요하므로 ncnn 파이썬 바인딩으로 직접 추론한다.
    Colab에서 `yolo export format=ncnn`으로 만든 폴더를 그대로 쓴다.
    Pi 4 실측: 416px / 3스레드 = 5.3 fps (claude/pi4-추론-성능-실측.md)

역할은 v2와 동일:
    카메라 → YOLO 추론 → (1) 정규화 박스 + 자세 를 WebSocket(:8765/ws)
                        (2) 박스를 그린 영상을 MJPEG(:8081/stream)

실행 예:
  # Pi 4 + mavlink-router(14551) + NCNN 모델
  python detector_node.py --model ~/vision/models/best416_ncnn --imgsz 416 \
                          --mavlink udpin:127.0.0.1:14551

  python detector_node.py --backend ultralytics --model yolov8n.pt   # 기존 방식
  python detector_node.py --source 0                                  # USB 웹캠
  python detector_node.py --mock                                      # 카메라·YOLO 없이
"""
import argparse
import asyncio
import base64
import collections
import json
import math
import os
import random
import signal
import subprocess
import threading
import time

import cv2
import numpy as np
from aiohttp import web

PERSON_NAMES = {"person", "pedestrian", "people"}
VEHICLE_NAMES = {"car", "truck", "bus", "motorcycle", "motor", "van", "vehicle"}

# COCO id → 우리 분류 (ncnn 백엔드용 — 모델이 클래스 이름을 안 갖고 있다)
COCO_KEEP = {0: "person", 2: "vehicle", 3: "vehicle", 5: "vehicle", 7: "vehicle"}
COCO_RAW = {0: "person", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


class Slot:
    """크기 1짜리 최신값 슬롯 — 큐를 쓰면 추론이 느릴 때 지연이 누적된다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._val = None
        self._ver = 0

    def put(self, val):
        with self._lock:
            self._val = val
            self._ver += 1

    def get(self):
        with self._lock:
            return self._val, self._ver


# ================================================================ ① MAVLink
FIX_TEXT = {0: "NO FIX", 1: "NO FIX", 2: "2D FIX", 3: "3D FIX",
            4: "DGPS", 5: "RTK FLOAT", 6: "RTK FIXED"}


def _wrap_pi(d):
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    return d


class AttitudeBuffer:
    """
    모노토닉 시각 기준 자세 링버퍼.

    프레임 시각과 자세 수신 시각을 같은 시계로 찍어두고 보간하는 것이 핵심이다.
    고도 80m에서 자세 1° 오차 = 지상 1.4m이므로, 20Hz(50ms 간격)를 보간하면
    20°/s 회전 중에도 오차 기여가 1° 아래로 떨어진다.
    """

    def __init__(self, maxlen=400):
        self._lock = threading.Lock()
        self._buf = collections.deque(maxlen=maxlen)   # (t_mono, roll, pitch, yaw) [rad]

    def add(self, t, roll, pitch, yaw):
        with self._lock:
            self._buf.append((t, roll, pitch, yaw))

    def stats(self):
        with self._lock:
            n = len(self._buf)
            if n < 2:
                return {"n": n, "hz": None}
            span = self._buf[-1][0] - self._buf[0][0]
            return {"n": n, "hz": round((n - 1) / span, 1) if span > 0 else None}

    def at(self, t):
        """t 시각의 자세를 선형 보간. 반환 (roll_deg, pitch_deg, yaw_deg) 또는 None."""
        with self._lock:
            buf = list(self._buf)
        if not buf:
            return None

        if t <= buf[0][0]:
            _, r, p, y = buf[0]
        elif t >= buf[-1][0]:
            _, r, p, y = buf[-1]
        else:
            r = p = y = None
            for i in range(len(buf) - 1, 0, -1):
                t0, r0, p0, y0 = buf[i - 1]
                t1, r1, p1, y1 = buf[i]
                if t0 <= t <= t1:
                    a = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                    r = r0 + a * _wrap_pi(r1 - r0)
                    p = p0 + a * _wrap_pi(p1 - p0)
                    y = y0 + a * _wrap_pi(y1 - y0)   # yaw는 ±π 경계를 넘나든다
                    break
            if r is None:
                _, r, p, y = buf[-1]

        return (round(math.degrees(r), 3),
                round(math.degrees(p), 3),
                round(math.degrees(y) % 360.0, 3))


def _request_rates(m):
    """
    이 보드(KoaFC/F405)에는 SR2_* 파라미터가 없으므로 런타임으로 주기를 요청한다.
    FC를 재부팅하면 초기화되므로 링크가 끊겼다 붙을 때마다 다시 보낸다.
    """
    from pymavlink import mavutil
    wanted = [
        (mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE, 20),
        (mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, 10),
        (mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT, 2),
        (mavutil.mavlink.MAVLINK_MSG_ID_SYS_STATUS, 2),
    ]
    for msg_id, hz in wanted:
        m.mav.command_long_send(
            m.target_system, 1,
            mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
            msg_id, int(1_000_000 / hz), 0, 0, 0, 0, 0)


def mavlink_loop(args, att_buf, pos_slot, stop):
    from pymavlink import mavutil

    while not stop.is_set():
        try:
            m = mavutil.mavlink_connection(args.mavlink)
            m.wait_heartbeat()
            print(f"[mav] heartbeat  sys={m.target_system}  ({args.mavlink})")
            _request_rates(m)
            last_req = time.monotonic()

            state = {"lat": None, "lon": None, "alt_rel": None, "gps": "NO FIX",
                     "battery": None, "speed": None, "mode": None, "t": 0.0,
                     "sats": 0, "armed": False}

            while not stop.is_set():
                msg = m.recv_match(blocking=True, timeout=1.0)
                if msg is None:
                    if time.monotonic() - last_req > 30:
                        _request_rates(m)
                        last_req = time.monotonic()
                    continue

                t = time.monotonic()
                typ = msg.get_type()

                if typ == "ATTITUDE":
                    att_buf.add(t, msg.roll, msg.pitch, msg.yaw)
                elif typ == "GLOBAL_POSITION_INT":
                    state["lat"] = round(msg.lat / 1e7, 7)
                    state["lon"] = round(msg.lon / 1e7, 7)
                    state["alt_rel"] = round(msg.relative_alt / 1000.0, 2)
                    state["speed"] = round(math.hypot(msg.vx, msg.vy) / 100.0, 2)
                    state["t"] = t
                    pos_slot.put(dict(state))
                elif typ == "GPS_RAW_INT":
                    state["gps"] = FIX_TEXT.get(msg.fix_type, "NO FIX")
                    state["sats"] = msg.satellites_visible
                    pos_slot.put(dict(state))
                elif typ == "SYS_STATUS":
                    b = msg.battery_remaining
                    state["battery"] = int(b) if b is not None and b >= 0 else None
                elif typ == "HEARTBEAT":
                    # 미션플래너(GCS, type 6)·mavlink-router 등 다른 장치의 하트비트는 무시.
                    # 이걸 안 거르면 대시보드 모드가 'Mode(0x00000000)'로 깜빡인다.
                    if msg.get_srcComponent() != 1 or msg.type == 6 or msg.autopilot == 8:
                        continue
                    try:
                        state["mode"] = mavutil.mode_string_v10(msg)
                    except Exception:
                        pass
                    state["armed"] = bool(msg.base_mode & 128)

        except Exception as e:
            print("[mav] 재연결 대기:", e)
            stop.wait(3)


# ================================================================ capture
CAM = {}   # 녹화 스레드가 같은 카메라 객체를 쓰기 위해 보관
def open_picamera(args):
    from picamera2 import Picamera2

    picam2 = Picamera2()
    cfg_kwargs = dict(main={"size": (args.width, args.height), "format": "RGB888"})

    # 풀 FOV 센서 모드(2304x1296) 명시.
    # 크롭 모드(1536x864)가 잡히면 화각이 66° → 47°로 줄어 위치추정이 통째로 틀어진다.
    cfg = None
    for attempt in (
        lambda: picam2.create_video_configuration(sensor={"output_size": (2304, 1296)}, **cfg_kwargs),
        lambda: picam2.create_video_configuration(raw={"size": (2304, 1296)}, **cfg_kwargs),
        lambda: picam2.create_video_configuration(**cfg_kwargs),
    ):
        try:
            cfg = attempt()
            break
        except (TypeError, KeyError, ValueError):
            continue

    if args.hflip or args.vflip:
        from libcamera import Transform
        cfg["transform"] = Transform(hflip=int(args.hflip), vflip=int(args.vflip))

    picam2.configure(cfg)
    picam2.start()

    try:
        full = picam2.camera_properties.get("ScalerCropMaximum")
        if full:
            picam2.set_controls({"ScalerCrop": full})
    except Exception:
        pass

    if args.lens_pos >= 0:
        try:
            from libcamera import controls as lc
            picam2.set_controls({"AfMode": lc.AfModeEnum.Manual, "LensPosition": args.lens_pos})
            print(f"[cam] 초점 고정 LensPosition={args.lens_pos} (0.0 = 무한대)")
        except Exception as e:
            print("[cam] 초점 고정 실패 (AF 없는 카메라?):", e)

    if args.shutter_us > 0:
        try:
            picam2.set_controls({"ExposureTime": int(args.shutter_us)})   # 게인은 자동으로 보충
            print(f"[cam] 셔터 고정 {int(args.shutter_us)} us (1/{int(1e6 / args.shutter_us)}초), 밝기는 게인 자동")
        except Exception as e:
            print("[cam] 셔터 고정 실패:", e)

    actual = picam2.camera_configuration()
    print("[cam] main :", actual["main"]["size"])
    raw = actual.get("raw") or actual.get("sensor")
    print("[cam] raw  :", raw.get("size") if raw else "?", " ← 2304x1296 이어야 풀 화각 66°")
    return picam2


SHARP = {"slot": None}   # 추론용 슬롯 (선명한 프레임 선택 시)
LIVE = {}                # 녹화 csv용 최신 성능 값 (추론·카메라·SysStats)


def sharpness(frame):
    """라플라시안 분산 — 클수록 선명. 320 폭으로 줄여 계산 (프레임당 1 ms 안팎)"""
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (320, max(1, int(h * 320 / w))), interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_32F).var())


class SharpPicker:
    """최근 N장 중 가장 선명한 프레임을 추론 슬롯에 넣고, 10초마다 통계 출력"""

    def __init__(self, n):
        self.n = max(1, int(n))
        self.win = collections.deque(maxlen=self.n)
        self.hist = collections.deque(maxlen=600)
        self.t_log = time.monotonic()

    def push(self, frame, t, md=None):
        s = sharpness(frame)
        self.hist.append(s)
        LIVE["sharp"] = round(s, 1)
        if md:
            if md.get("ExposureTime"):
                LIVE["exp_us"] = int(md["ExposureTime"])
            if md.get("AnalogueGain"):
                LIVE["gain"] = round(float(md["AnalogueGain"]), 2)
        self.win.append((s, frame, t))
        if SHARP["slot"] is not None:
            best = max(self.win, key=lambda x: x[0])
            SHARP["slot"].put((best[1], best[2]))
        now = time.monotonic()
        if now - self.t_log >= 10 and len(self.hist) > 20:
            arr = np.array(self.hist)
            ref = np.percentile(arr, 95)
            blur = float((arr < 0.3 * ref).mean() * 100)
            extra = ""
            if md:
                et, ag = md.get("ExposureTime"), md.get("AnalogueGain")
                if et:
                    extra = f" 노출 1/{int(1e6 / et)}초({et} us) 게인 {ag:.1f}" if ag else f" 노출 {et} us"
            print(f"[cam]{extra} 선명도 중앙값 {np.median(arr):.0f}, 흐린 프레임(상위5% 대비 30% 미만) {blur:.0f}%"
                  + (f", 추론은 최근 {self.n}장 중 가장 선명한 프레임" if SHARP["slot"] is not None else ""))
            self.t_log = now


def capture_loop(args, frame_slot, stop):
    picker = SharpPicker(args.sharp_window)
    if args.source == "picam":
        cam = open_picamera(args)
        CAM["cam"] = cam
        while not stop.is_set():
            req = cam.capture_request()
            try:
                frame = req.make_array("main")   # RGB888 설정이지만 numpy 상으로는 BGR 순서
                md = req.get_metadata()
            finally:
                req.release()
            t = time.monotonic()
            frame_slot.put((frame, t))
            picker.push(frame, t, md)
        cam.stop()
        return

    src = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(src)
    if isinstance(src, int):
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    is_file = not isinstance(src, int)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30

    while not stop.is_set():
        ok, frame = cap.read()
        if not ok:
            if is_file:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            time.sleep(0.05)
            continue
        if args.hflip:
            frame = cv2.flip(frame, 1)
        if args.vflip:
            frame = cv2.flip(frame, 0)
        t = time.monotonic()
        frame_slot.put((frame, t))
        picker.push(frame, t)
        if is_file:
            time.sleep(1.0 / fps)
    cap.release()


def mock_capture_loop(args, frame_slot, stop):
    while not stop.is_set():
        img = np.full((args.height, args.width, 3), 40, np.uint8)
        cv2.putText(img, "MOCK CAMERA " + time.strftime("%H:%M:%S"), (40, 80),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.6, (200, 200, 200), 3)
        frame_slot.put((img, time.monotonic()))
        time.sleep(1 / 20)


# ================================================================ 공통 유틸

# ================================================================ 원본 녹화 (--record)
def record_loop(args, frame_slot, pos_slot, att_buf, stop):
    """박스·글씨 없는 카메라 원본을 mp4로, 같은 시각의 위치·자세를 csv로 저장.

    - picam이면 파이의 하드웨어 H.264 인코더(Picamera2) 사용 → CPU 거의 안 씀
    - 실패하거나 웹캠·동영상 입력이면 OpenCV로 --record-fps 만큼만 저장 (CPU 조금 씀)
    - --record-minutes 마다 새 파일 (전원이 갑자기 꺼져도 앞 파일은 살아 있음)
    - --record-armed 면 ARM 동안만 저장, 남은 용량이 --record-min-free GB 밑이면 멈춤
    """
    os.makedirs(args.record, exist_ok=True)
    seg_len = args.record_minutes * 60.0
    cur = None

    def want():
        if not args.record_armed:
            return True
        p, _ = pos_slot.get()
        return bool(p and p.get("armed"))

    def free_gb():
        st = os.statvfs(args.record)
        return st.f_bavail * st.f_frsize / 1e9

    def open_seg():
        name = time.strftime("rec_%Y%m%d_%H%M%S")
        path = os.path.join(args.record, name + ".mp4")
        seg = {"t0": time.monotonic(), "path": path, "n": 0}
        cam = CAM.get("cam")
        if cam is not None:
            try:
                from picamera2.encoders import H264Encoder
                from picamera2.outputs import FfmpegOutput
                enc = H264Encoder(bitrate=args.record_bitrate)
                cam.start_encoder(enc, FfmpegOutput(path), name="main")
                seg.update(mode="hw", enc=enc, cam=cam)
            except Exception as e:
                print("[rec] 하드웨어 인코더 실패 → OpenCV로 저장:", e)
        if "mode" not in seg:
            seg.update(mode="cv2", writer=None)
        seg["csv"] = open(os.path.join(args.record, name + ".csv"), "w", encoding="utf-8")
        seg["csv"].write("t_s,time,lat,lon,alt_rel_m,roll_deg,pitch_deg,yaw_deg,mode,armed,sats,gps,"
                         "fps,infer_ms,temp_c,cpu_pct,throttled,exp_us,gain,sharp\n")
        print(f"[rec] 녹화 시작 {path} ({'하드웨어 H.264' if seg['mode'] == 'hw' else f'OpenCV {args.record_fps} fps'})")
        return seg

    def close_seg(seg):
        try:
            if seg["mode"] == "hw":
                try:
                    seg["cam"].stop_encoder(seg["enc"])
                except TypeError:
                    seg["cam"].stop_encoder()
            elif seg.get("writer") is not None:
                seg["writer"].release()
        except Exception as e:
            print("[rec] 종료 중 오류:", e)
        seg["csv"].close()
        print(f"[rec] 저장 완료 {seg['path']} ({time.monotonic() - seg['t0']:.0f}초)")

    last_meta, last_frame_ver = 0.0, -1
    while not stop.is_set():
        ok = want()
        if ok and free_gb() < args.record_min_free:
            if cur:
                print(f"[rec] 남은 용량 {free_gb():.1f} GB — 녹화 중지")
            ok = False
        if cur and (not ok or time.monotonic() - cur["t0"] > seg_len):
            close_seg(cur)
            cur = None
        if ok and cur is None:
            try:
                cur = open_seg()
            except Exception as e:
                print("[rec] 녹화 시작 실패:", e)
                stop.wait(5)
                continue
        if cur is None:
            stop.wait(0.5)
            continue

        now = time.monotonic()
        if cur["mode"] == "cv2":
            item, ver = frame_slot.get()
            if item is not None and ver != last_frame_ver:
                frame = item[0]
                if cur["writer"] is None:
                    h, w = frame.shape[:2]
                    cur["writer"] = cv2.VideoWriter(cur["path"], cv2.VideoWriter_fourcc(*"mp4v"),
                                                    args.record_fps, (w, h))
                cur["writer"].write(frame)
                last_frame_ver = ver

        if now - last_meta >= 0.2:      # 위치·자세 5 Hz
            p, _ = pos_slot.get()
            p = p or {}
            sd = (LIVE.get("sys").d if LIVE.get("sys") else {}) or {}
            a = att_buf.at(now) if att_buf else None
            f = lambda v: "" if v is None else v
            cur["csv"].write(",".join(str(x) for x in (
                round(now - cur["t0"], 3), time.strftime("%H:%M:%S"),
                f(p.get("lat")), f(p.get("lon")), f(p.get("alt_rel")),
                *(("", "", "") if a is None else tuple(round(v, 2) for v in a)),
                f(p.get("mode")), int(bool(p.get("armed"))), f(p.get("sats")), f(p.get("gps")),
                *(f(LIVE.get(k)) for k in ("fps", "infer_ms")),
                *(f(sd.get(k)) for k in ("temp_c", "cpu_pct", "throttled")),
                *(f(LIVE.get(k)) for k in ("exp_us", "gain", "sharp")))) + "\n")
            last_meta = now
        stop.wait(1.0 / args.record_fps if cur["mode"] == "cv2" else 0.2)

    if cur:
        close_seg(cur)

def make_crop(frame, b, max_side=160):
    """박스 주변을 여유 있게 잘라 작은 JPEG(base64)로 — 스냅샷 갤러리용."""
    h, w = frame.shape[:2]
    cx, cy = (b["x"] + b["w"] / 2) * w, (b["y"] + b["h"] / 2) * h
    side = max(b["w"] * w, b["h"] * h) * 1.6 + 24
    x1, y1 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    x2, y2 = int(min(w, cx + side / 2)), int(min(h, cy + side / 2))
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    s = max_side / max(crop.shape[:2])
    if s < 1:
        crop = cv2.resize(crop, (int(crop.shape[1] * s), int(crop.shape[0] * s)))
    ok, jpg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return base64.b64encode(jpg.tobytes()).decode() if ok else None


def map_class(name):
    n = name.lower()
    if n in PERSON_NAMES:
        return "person"
    if n in VEHICLE_NAMES:
        return "vehicle"
    return None


def attach_context(det, att_buf, pos_slot, t_cap):
    """프레임 시각에 맞춘 자세 + 최신 위치를 탐지 결과에 붙인다."""
    att = att_buf.at(t_cap) if att_buf is not None else None
    if att:
        det["att"] = {"roll": att[0], "pitch": att[1], "yaw": att[2], "src": "pi_uart"}
        det["att_stats"] = att_buf.stats()
    else:
        det["att"] = None
    pos, _ = pos_slot.get()
    det["pos"] = pos
    return det


# ================================================================ ② 추론 — raw ncnn
def letterbox(img_bgr, size):
    """종횡비 유지 + 회색 패딩. (canvas, (scale, pad_x, pad_y, src_w, src_h)) 반환."""
    h, w = img_bgr.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(img_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    pad_x, pad_y = (size - nw) // 2, (size - nh) // 2
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return canvas, (scale, pad_x, pad_y, w, h)


def nms(boxes, scores, iou_th):
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_th]
    return keep


def load_class_map(model_dir):
    """모델 폴더의 metadata.yaml(ultralytics export가 함께 만든다)에서 클래스 이름을 읽어
    {모델 클래스 id: (우리 분류, 원래 이름)} 을 만든다.
    - COCO 모델: person / car·motorcycle·bus·truck
    - VisDrone 모델: pedestrian·people → 사람, car·van·truck·bus·motor → 차량
    metadata.yaml이 없으면 COCO로 가정 (v3 동작)."""
    names = {}
    path = os.path.join(model_dir, "metadata.yaml")
    try:
        in_names = False
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("names:"):
                    in_names = True
                    continue
                if in_names:
                    if not line.startswith(" "):
                        break
                    k, _, v = line.strip().partition(":")
                    if k.strip().isdigit():
                        names[int(k)] = v.strip().strip("'\"")
    except OSError:
        pass
    if not names:
        print("[ncnn] metadata.yaml 없음 — COCO 클래스로 가정")
        return 80, {i: (c, COCO_RAW[i]) for i, c in COCO_KEEP.items()}
    keep = {i: (map_class(n), n) for i, n in names.items() if map_class(n)}
    print(f"[ncnn] 클래스 {len(names)}개 중 사용: {[keep[i][1] for i in sorted(keep)]}")
    return len(names), keep


CLASS_NC, CLASS_KEEP = 80, {i: (c, COCO_RAW[i]) for i, c in COCO_KEEP.items()}


def decode_yolov8(out_mat, meta, conf_th, iou_th):
    """ultralytics NCNN export 출력(out0: (4+클래스수) x N) → 정규화 박스 리스트."""
    rows = 4 + CLASS_NC
    keep_ids = np.array(sorted(CLASS_KEEP))
    a = np.array(out_mat, dtype=np.float64)
    if a.ndim == 3:
        a = a[0]
    if a.shape[0] != rows:
        a = a.T
    if a.shape[0] != rows:
        return []

    xywh = a[:4]
    cls_scores = a[4:][keep_ids]
    best = cls_scores.argmax(axis=0)
    conf = cls_scores.max(axis=0)

    m = conf > conf_th
    if not m.any():
        return []

    cx, cy, bw, bh = xywh[:, m]
    conf = conf[m]
    cls_id = keep_ids[best[m]]

    xyxy = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
    keep = nms(xyxy, conf, iou_th)

    scale, pad_x, pad_y, src_w, src_h = meta
    out = []
    for i in keep:
        bx1 = (xyxy[i, 0] - pad_x) / scale
        by1 = (xyxy[i, 1] - pad_y) / scale
        bx2 = (xyxy[i, 2] - pad_x) / scale
        by2 = (xyxy[i, 3] - pad_y) / scale

        nx = min(max(bx1 / src_w, 0.0), 1.0)
        ny = min(max(by1 / src_h, 0.0), 1.0)
        nw = min(max((bx2 - bx1) / src_w, 0.0), 1.0)
        nh = min(max((by2 - by1) / src_h, 0.0), 1.0)
        if nw <= 0 or nh <= 0:
            continue

        cid = int(cls_id[i])
        out.append({
            "cls": CLASS_KEEP[cid][0],
            "raw_cls": CLASS_KEEP[cid][1],
            "conf": round(float(conf[i]), 3),
            "x": round(nx, 4), "y": round(ny, 4),
            "w": round(nw, 4), "h": round(nh, 4),
        })
    return out


def inference_loop_ncnn(args, frame_slot, det_slot, att_buf, pos_slot, stop):
    import ncnn

    cv2.setNumThreads(1)          # OpenCV 스레드가 NCNN과 코어를 두고 싸우지 않게

    mdir = os.path.expanduser(args.model)
    param = os.path.join(mdir, "model.ncnn.param")
    binf = os.path.join(mdir, "model.ncnn.bin")

    net = ncnn.Net()
    net.opt.num_threads = args.threads
    net.load_param(param)
    net.load_model(binf)
    global CLASS_NC, CLASS_KEEP
    CLASS_NC, CLASS_KEEP = load_class_map(mdir)
    print(f"[ncnn] {mdir}  imgsz={args.imgsz}  threads={args.threads}")

    last_ver = -1
    seq = 0
    t_hist = []

    while not stop.is_set():
        item, ver = frame_slot.get()
        if item is None or ver == last_ver:
            time.sleep(0.005)
            continue
        last_ver = ver
        frame, t_cap = item

        t0 = time.monotonic()
        padded, meta = letterbox(frame, args.imgsz)
        padded = np.ascontiguousarray(padded)

        mat = ncnn.Mat.from_pixels(
            padded, ncnn.Mat.PixelType.PIXEL_BGR2RGB, args.imgsz, args.imgsz)
        mat.substract_mean_normalize([], [1 / 255.0] * 3)

        try:
            ex = net.create_extractor()
            ex.input("in0", mat)
            ret, out = ex.extract("out0")
            if ret != 0:
                continue
            boxes = decode_yolov8(out, meta, args.conf, args.iou)
        except Exception as e:
            print("[ncnn] error:", e)
            time.sleep(0.05)
            continue

        now = time.monotonic()
        t_hist = [t for t in t_hist if now - t < 2.0] + [now]
        seq += 1

        if args.crops:
            for bx in boxes[:args.max_crops]:
                bx["crop"] = make_crop(frame, bx)

        LIVE["infer_ms"] = round((now - t0) * 1000, 1)
        LIVE["fps"] = round(len(t_hist) / 2.0, 1)
        det_slot.put(attach_context({
            "seq": seq, "t_cap": t_cap, "boxes": boxes,
            "infer_ms": LIVE["infer_ms"],
            "fps": LIVE["fps"],
        }, att_buf, pos_slot, t_cap))


# ================================================================ 추론 — ultralytics (기존)
def inference_loop_ultralytics(args, frame_slot, det_slot, att_buf, pos_slot, stop):
    from ultralytics import YOLO

    model = YOLO(args.model, task="detect")
    names = model.names
    keep_ids = [i for i, n in names.items() if map_class(n)]
    print(f"[yolo] 모델 {args.model} 로드, 사용 클래스: {[names[i] for i in keep_ids]}")

    last_ver = -1
    seq = 0
    t_hist = []

    while not stop.is_set():
        item, ver = frame_slot.get()
        if item is None or ver == last_ver:
            time.sleep(0.005)
            continue
        last_ver = ver
        frame, t_cap = item
        h, w = frame.shape[:2]

        t0 = time.monotonic()
        res = model.predict(frame, imgsz=args.imgsz, conf=args.conf,
                            classes=keep_ids, verbose=False)[0]
        boxes = []
        for b in res.boxes:
            cls = map_class(names[int(b.cls)])
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            boxes.append({
                "cls": cls, "raw_cls": names[int(b.cls)],
                "conf": round(float(b.conf), 3),
                "x": round(x1 / w, 4), "y": round(y1 / h, 4),
                "w": round((x2 - x1) / w, 4), "h": round((y2 - y1) / h, 4),
            })

        now = time.monotonic()
        t_hist = [t for t in t_hist if now - t < 2.0] + [now]
        seq += 1

        if args.crops:
            for bx in boxes[:args.max_crops]:
                bx["crop"] = make_crop(frame, bx)

        LIVE["infer_ms"] = round((now - t0) * 1000, 1)
        LIVE["fps"] = round(len(t_hist) / 2.0, 1)
        det_slot.put(attach_context({
            "seq": seq, "t_cap": t_cap, "boxes": boxes,
            "infer_ms": LIVE["infer_ms"],
            "fps": LIVE["fps"],
        }, att_buf, pos_slot, t_cap))


def mock_inference_loop(args, frame_slot, det_slot, att_buf, pos_slot, stop):
    seq = 0
    while not stop.is_set():
        seq += 1
        t = time.time()
        boxes = [
            {"cls": "person", "raw_cls": "person", "conf": 0.81,
             "x": 0.30 + 0.05 * math.sin(t / 3), "y": 0.40, "w": 0.04, "h": 0.08},
            {"cls": "vehicle", "raw_cls": "car", "conf": 0.77,
             "x": 0.65, "y": 0.55 + 0.05 * math.cos(t / 4), "w": 0.10, "h": 0.07},
        ]
        if random.random() < 0.3:
            boxes.pop()
        if args.crops:
            item, _ = frame_slot.get()
            if item is not None:
                for bx in boxes:
                    bx["crop"] = make_crop(item[0], bx)
        t_cap = time.monotonic()
        det_slot.put(attach_context(
            {"seq": seq, "t_cap": t_cap, "boxes": boxes, "infer_ms": 0, "fps": 5.0},
            att_buf, pos_slot, t_cap))
        time.sleep(0.2)


# ================================================================ system stats
class SysStats:
    """Pi 온도 / 스로틀 / CPU 사용률 — 2초마다 갱신."""

    def __init__(self):
        self.d = {"temp_c": None, "throttled": None, "cpu_pct": None}
        self._prev = None

    def _cpu(self):
        try:
            with open("/proc/stat") as f:
                v = [int(x) for x in f.readline().split()[1:]]
            idle, total = v[3] + v[4], sum(v)
            if self._prev:
                di, dt = idle - self._prev[0], total - self._prev[1]
                self.d["cpu_pct"] = round(100 * (1 - di / dt), 1) if dt else None
            self._prev = (idle, total)
        except OSError:
            pass

    def loop(self, stop):
        while not stop.is_set():
            self._cpu()
            try:
                with open("/sys/class/thermal/thermal_zone0/temp") as f:
                    self.d["temp_c"] = round(int(f.read()) / 1000, 1)
            except OSError:
                pass
            try:
                out = subprocess.run(["vcgencmd", "get_throttled"],
                                     capture_output=True, text=True, timeout=2).stdout
                self.d["throttled"] = out.strip().split("=")[-1] if out else None
            except (OSError, subprocess.SubprocessError):
                pass
            stop.wait(2)


# ================================================================ serving
COLORS = {"person": (80, 80, 255), "vehicle": (255, 190, 60)}


def draw(frame, det):
    img = frame.copy()
    if not det:
        return img
    h, w = img.shape[:2]
    for b in det["boxes"]:
        x1, y1 = int(b["x"] * w), int(b["y"] * h)
        x2, y2 = int((b["x"] + b["w"]) * w), int((b["y"] + b["h"]) * h)
        c = COLORS.get(b["cls"], (0, 255, 0))
        cv2.rectangle(img, (x1, y1), (x2, y2), c, 2)
        cv2.putText(img, f'{b["cls"]} {b["conf"]:.2f}', (x1, max(18, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, c, 2)

    att = det.get("att")
    line = f'FPS {det["fps"]}  seq {det["seq"]}'
    if att:
        line += f'  R{att["roll"]:+.1f} P{att["pitch"]:+.1f} Y{att["yaw"]:.1f}'
    cv2.putText(img, line, (10, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return img


def build_app(args, frame_slot, det_slot, sysstats):
    clients = set()

    async def mjpeg(request):
        resp = web.StreamResponse(headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=frame",
            "Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*",
        })
        await resp.prepare(request)
        last = -1
        try:
            while True:
                item, ver = frame_slot.get()
                if item is None or ver == last:
                    await asyncio.sleep(0.01)
                    continue
                last = ver
                det, _ = det_slot.get()
                img = draw(item[0], det)
                ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality])
                if ok:
                    await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                                     + jpg.tobytes() + b"\r\n")
                await asyncio.sleep(1.0 / args.stream_fps)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        clients.add(ws)
        print(f"[ws] 접속: {request.remote} (총 {len(clients)})")
        try:
            async for m in ws:
                if m.type == web.WSMsgType.TEXT and '"ping"' in m.data:
                    try:
                        await ws.send_str(json.dumps(
                            {"type": "pong", "id": json.loads(m.data).get("id")}))
                    except Exception:
                        pass
        finally:
            clients.discard(ws)
            print(f"[ws] 해제: {request.remote}")
        return ws

    async def health(request):
        det, _ = det_slot.get()
        return web.json_response(
            {"ok": True,
             "fps": det["fps"] if det else 0,
             "boxes": len(det["boxes"]) if det else 0,
             "att": det.get("att") if det else None,
             "pos": det.get("pos") if det else None},
            headers={"Access-Control-Allow-Origin": "*"})

    async def broadcaster(app):
        async def loop():
            last = -1
            while True:
                await asyncio.sleep(1.0 / args.ws_hz)
                det, ver = det_slot.get()
                if det is None or ver == last:
                    continue
                last = ver
                msg = json.dumps({
                    "type": "detections", "seq": det["seq"], "fps": det["fps"],
                    "infer_ms": det["infer_ms"],
                    # 캡처 후 경과 시간. att가 있으면 지상국은 이걸로 되짚을 필요가 없다.
                    "age_ms": round((time.monotonic() - det["t_cap"]) * 1000, 1),
                    "img_w": args.width, "img_h": args.height,
                    "boxes": det["boxes"],
                    # ★ 신규 — 프레임 시각에 보간된 자세 + 그때의 기체 위치
                    "att": det.get("att"),
                    "att_stats": det.get("att_stats"),
                    "pos": det.get("pos"),
                    "sys": sysstats.d,
                })
                for ws in list(clients):
                    try:
                        await ws.send_str(msg)
                    except Exception:
                        clients.discard(ws)
        task = asyncio.create_task(loop())
        yield
        task.cancel()

    app = web.Application()
    app.router.add_get("/stream", mjpeg)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/", ws_handler)
    app.router.add_get("/health", health)
    app.cleanup_ctx.append(broadcaster)
    return app


async def serve(app, args):
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", args.video_port).start()
    await web.TCPSite(runner, "0.0.0.0", args.ws_port).start()
    print(f"[srv] MJPEG  http://<파이IP>:{args.video_port}/stream")
    print(f"[srv] WS     ws://<파이IP>:{args.ws_port}/ws")
    print("[srv] 파이 IP 확인: hostname -I")
    while True:
        await asyncio.sleep(3600)


def pick_backend(args):
    if args.backend != "auto":
        return args.backend
    mdir = os.path.expanduser(args.model)
    if os.path.isdir(mdir) and os.path.exists(os.path.join(mdir, "model.ncnn.param")):
        return "ncnn"
    return "ultralytics"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="picam", help="picam | 웹캠번호(0) | 동영상 경로")
    p.add_argument("--model", default="yolov8n.pt",
                   help="NCNN 모델 폴더 경로 | yolov8n.pt")
    p.add_argument("--backend", default="auto", choices=["auto", "ncnn", "ultralytics"],
                   help="auto면 모델 폴더에 model.ncnn.param이 있을 때 ncnn 사용")
    p.add_argument("--threads", type=int, default=3, help="NCNN 추론 스레드 (Pi 4 실측 권장 3)")
    p.add_argument("--imgsz", type=int, default=416)
    p.add_argument("--conf", type=float, default=0.35)
    p.add_argument("--iou", type=float, default=0.45)
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--hflip", action="store_true")
    p.add_argument("--vflip", action="store_true")
    p.add_argument("--video-port", type=int, default=8081)
    p.add_argument("--ws-port", type=int, default=8765)
    p.add_argument("--ws-hz", type=float, default=5.0)
    p.add_argument("--stream-fps", type=float, default=15.0)
    p.add_argument("--jpeg-quality", type=int, default=70)
    p.add_argument("--no-crops", dest="crops", action="store_false", help="스냅샷용 크롭 전송 끄기")
    p.add_argument("--max-crops", type=int, default=6)
    p.add_argument("--mock", action="store_true", help="카메라·YOLO 없이 가짜 영상/박스")
    p.add_argument("--lens-pos", type=float, default=0.0,
                   help="카메라 모듈 3 초점 고정값 (0.0=무한대, 비행용). -1이면 자동초점 유지")
    p.add_argument("--shutter-us", type=float, default=0,
                   help="셔터 시간 고정(마이크로초). 2000 = 1/500초. 0이면 자동노출 (기본)")
    p.add_argument("--sharp-window", type=int, default=4,
                   help="최근 N장 중 가장 선명한 프레임을 추론 (1 = 끔, 기본 4)")
    p.add_argument("--mavlink", default=None,
                   help="파이가 FC 자세를 직접 받을 경로. 예: udpin:127.0.0.1:14551 "
                        "(mavlink-router 경유). 생략하면 자세를 보내지 않는다(v2 동작)")
    p.add_argument("--record", default=None, metavar="폴더",
                   help="원본 영상(박스 없음)+위치·자세 csv 저장 폴더. 예: ~/rec  (생략하면 저장 안 함)")
    p.add_argument("--record-armed", action="store_true", help="ARM 동안만 저장 (--mavlink 필요)")
    p.add_argument("--record-minutes", type=float, default=5.0, help="이 시간마다 새 파일")
    p.add_argument("--record-bitrate", type=int, default=8_000_000, help="하드웨어 인코더 비트레이트")
    p.add_argument("--record-fps", type=float, default=10.0, help="OpenCV 대체 저장 시 초당 프레임")
    p.add_argument("--record-min-free", type=float, default=1.0, help="남은 용량(GB)이 이보다 적으면 중지")
    args = p.parse_args()
    if args.record:
        args.record = os.path.expanduser(args.record)
    if args.record_armed and not args.mavlink:
        print("[rec] --record-armed 는 --mavlink 가 있어야 ARM 상태를 안다 → 항상 저장으로 동작")
        args.record_armed = False

    frame_slot, det_slot, pos_slot = Slot(), Slot(), Slot()
    stop = threading.Event()
    sysstats = SysStats()
    LIVE["sys"] = sysstats
    att_buf = AttitudeBuffer() if args.mavlink else None

    threading.Thread(target=sysstats.loop, args=(stop,), daemon=True).start()

    if args.mavlink:
        threading.Thread(target=mavlink_loop,
                         args=(args, att_buf, pos_slot, stop), daemon=True).start()
    else:
        print("[mav] --mavlink 미지정 — 자세는 보내지 않는다 (지상국이 텔레메트리로 처리)")

    cap_fn = mock_capture_loop if args.mock else capture_loop
    if args.mock:
        inf_fn = mock_inference_loop
    else:
        backend = pick_backend(args)
        inf_fn = inference_loop_ncnn if backend == "ncnn" else inference_loop_ultralytics
        print(f"[cfg] 추론 백엔드: {backend}")

    infer_slot = frame_slot
    if args.sharp_window > 1 and not args.mock:
        infer_slot = Slot()
        SHARP["slot"] = infer_slot
        print(f"[cfg] 선명한 프레임 선택: 최근 {args.sharp_window}장 중 최선 → 추론")
    threading.Thread(target=cap_fn, args=(args, frame_slot, stop), daemon=True).start()
    threading.Thread(target=inf_fn,
                     args=(args, infer_slot, det_slot, att_buf, pos_slot, stop),
                     daemon=True).start()
    rec_thread = None
    if args.record:
        rec_thread = threading.Thread(target=record_loop,
                                      args=(args, frame_slot, pos_slot, att_buf, stop), daemon=True)
        rec_thread.start()

    # systemctl stop/restart(SIGTERM) 때도 아래 finally가 돌아 녹화 파일을 정상적으로 닫게 한다
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        asyncio.run(serve(build_app(args, frame_slot, det_slot, sysstats), args))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if rec_thread:
            rec_thread.join(timeout=5)   # 마지막 녹화 파일을 정상적으로 닫는다


if __name__ == "__main__":
    main()
