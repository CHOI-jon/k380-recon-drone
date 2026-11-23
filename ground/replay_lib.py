"""
replay_lib.py — 자료 보관함: 녹화 영상·비행 로그를 대시보드에서 바로 재생·분석

  ground/replays/ 폴더에 파일을 넣거나 대시보드에서 끌어다 놓으면 자동으로 정리·분석된다.
    · K380 원본 녹화   rec_YYYYmmdd_HHMMSS.mp4 + 같은 이름 .csv  (파이 --record)
    · 매빅(DJI) 영상   DJI_….MP4 (영상 안 비행 데이터 트랙 djmd 사용)
    · 비행 로그        ….bin (ArduPilot 데이터플래시)
  영상은 노트북에서 YOLO로 다시 탐지 → 비행 데이터로 위치추정 → 객체 묶기 → track.json
  (대시보드 replay.js가 영상 재생 시각에 맞춰 지도·목록·비행 상태를 보여 줌)
  로그는 진동·자세·배터리·모드·고속 진동 스펙트럼(FFT)을 요약 → log.json (log.html에서 표시)
"""
import asyncio
import bisect
import csv
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from aiohttp import web

import djimeta
from geolocate import Camera, pixel_to_geo

BASE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(BASE, "replays")
MODELS = os.path.join(BASE, "models")
WEB = os.path.join(BASE, "web")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
PERSON = {"pedestrian", "people", "person"}
VEHICLE = {"car", "van", "truck", "bus", "motor", "motorcycle"}
KIND_KO = {"k380": "K380 녹화", "mavic": "매빅 영상", "log": "비행 로그", "demo": "기본 데모", "result": "모델 결과 영상"}
MAVIC_HFOV = 24.2   # 0314 영상 트랙 레인 폭으로 보정한 값 (3배 망원 계열). 1배 광각이면 약 65
COPTER_MODES = {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED", 5: "LOITER", 6: "RTL",
                7: "CIRCLE", 9: "LAND", 11: "DRIFT", 13: "SPORT", 14: "FLIP", 15: "AUTOTUNE", 16: "POSHOLD",
                17: "BRAKE", 18: "THROW", 20: "GUIDED_NOGPS", 21: "SMART_RTL", 22: "FLOWHOLD",
                23: "FOLLOW", 24: "ZIGZAG", 25: "SYSTEMID", 27: "AUTO_RTL"}

os.makedirs(LIB, exist_ok=True)
os.makedirs(MODELS, exist_ok=True)
_lock = threading.RLock()


# ================================================================ 보관함 정리
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe(s):
    """폴더·파일 이름은 영문·숫자만 (윈도우 OpenCV가 한글 경로 영상을 못 여는 문제 방지)"""
    s = re.sub(r"[^0-9A-Za-z_.-]+", "_", s).strip("._")
    return (s or "item_" + time.strftime("%Y%m%d_%H%M%S"))[:60]


def _mpath(iid):
    return os.path.join(LIB, iid, "meta.json")


def load_meta(iid):
    try:
        with open(_mpath(iid), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_meta(m):
    p = _mpath(m["id"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def set_status(iid, **kw):
    with _lock:
        m = load_meta(iid)
        if not m:
            return
        m["status"].update(kw)
        save_meta(m)


def _start_from_name(name):
    m = re.search(r"(20\d{6})[_-]?(\d{6})", name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").isoformat()
        except ValueError:
            pass
    return None


_dji_cache = {}
_seen = {}        # 파일 이름 → ((크기, 수정시각), 그 상태가 처음 보인 시각) — 복사가 끝났는지 판단
_trusted = set()  # 대시보드로 올린 파일 (이미 다 받음)


def _is_dji(path):
    st = os.stat(path)
    key = (path, st.st_size, int(st.st_mtime))
    if key not in _dji_cache:
        _dji_cache[key] = djimeta.has_dji_track(path)
    return _dji_cache[key]


def _new_item(kind, stem, files, size):
    iid = _safe(stem)
    k = 2
    while os.path.exists(os.path.join(LIB, iid)):
        iid = f"{_safe(stem)}_{k}"
        k += 1
    d = os.path.join(LIB, iid)
    os.makedirs(d)
    moved, done = {}, []
    try:
        for role, src in files.items():
            stem_, ext = os.path.splitext(os.path.basename(src))
            name = _safe(stem_) + ext.lower()
            dst = os.path.join(d, name)
            os.replace(src, dst)        # 복사 중인 파일이면 윈도우에서 PermissionError
            done.append((dst, src))
            moved[role] = name
    except OSError:
        for dst, src in done:
            os.replace(dst, src)
        shutil.rmtree(d, ignore_errors=True)
        return None
    m = dict(id=iid, name=stem, kind=kind, created=_now(), files=moved, size=size,
             start=_start_from_name(stem), status=dict(state="new", progress=0, msg=""), params={}, result={})
    save_meta(m)
    return m


def _add(new, pending, name, m):
    if m:
        new.append(m["id"])
        _trusted.discard(name)
    else:
        pending.append(dict(name=name, reason="파일을 옮기지 못함 (아직 복사 중이거나 다른 프로그램이 사용 중) — 잠시 후 자동 재시도"))


def scan():
    """replays 폴더 바로 아래에 놓인 파일을 종류별 항목 폴더로 정리. 반환: (새 항목 id 목록, 보류 파일 목록)"""
    new, pending = [], []
    with _lock:
        try:
            names = os.listdir(LIB)
        except FileNotFoundError:
            return new, pending
        files = {}
        for n in names:
            p = os.path.join(LIB, n)
            if not os.path.isfile(p) or n.startswith(".") or n.endswith((".part", ".tmp")):
                continue
            st = os.stat(p)
            sig = (st.st_size, st.st_mtime)
            prev = _seen.get(n)
            _seen[n] = (sig, prev[1] if prev and prev[0] == sig else time.time())
            if n not in _trusted and (time.time() - _seen[n][1] < 2 or time.time() - st.st_mtime < 2):
                pending.append(dict(name=n, reason="복사 중… (끝나면 자동으로 정리됩니다)"))
                continue
            files[n] = st.st_size
        by_stem = {}
        for n in files:
            stem, ext = os.path.splitext(n)
            by_stem.setdefault(stem.lower(), {})[ext.lower()] = n
        for stem_l, grp in by_stem.items():
            vids = [grp[e] for e in grp if e in VIDEO_EXT]
            if ".bin" in grp:
                n = grp[".bin"]
                _add(new, pending, n, _new_item("log", os.path.splitext(n)[0], {"bin": os.path.join(LIB, n)}, files[n]))
            if vids:
                v = vids[0]
                vp = os.path.join(LIB, v)
                stem = os.path.splitext(v)[0]
                if ".csv" in grp:
                    c = grp[".csv"]
                    _add(new, pending, v, _new_item("k380", stem, {"video": vp, "csv": os.path.join(LIB, c)},
                                                    files[v] + files[c]))
                elif _is_dji(vp):
                    _add(new, pending, v, _new_item("mavic", stem, {"video": vp}, files[v]))
                elif stem.lower().startswith("rec_"):
                    pending.append(dict(name=v, reason="같은 이름의 .csv(위치·자세 기록)가 필요합니다"))
                else:
                    pending.append(dict(name=v, reason="위치 정보가 없는 영상 (K380 .csv 짝 또는 DJI 원본이 아님)"))
            elif ".csv" in grp:
                pending.append(dict(name=grp[".csv"], reason="같은 이름의 영상(.mp4)이 필요합니다"))
            for e, n in grp.items():
                if e not in VIDEO_EXT and e not in (".csv", ".bin"):
                    pending.append(dict(name=n, reason="지원하지 않는 파일 형식"))
    return new, pending


def list_items():
    out = []
    for n in os.listdir(LIB):
        if os.path.isdir(os.path.join(LIB, n)):
            m = load_meta(n)
            if m:
                d = os.path.join(LIB, n)
                m["has_track"] = os.path.exists(os.path.join(d, "track.json"))
                m["has_log"] = os.path.exists(os.path.join(d, "log.json"))
                m["kind_ko"] = KIND_KO.get(m["kind"], m["kind"])
                out.append(m)
    # 순서: 보관함에 넣은(만든) 시각이 최근일수록 1번
    out.sort(key=lambda m: (m.get("created") or "", m.get("start") or ""), reverse=True)   # 같은 시각에 넣었으면 촬영이 최근인 것 먼저
    for i, m in enumerate(out):
        m["rank"] = i + 1
    has_result = any(m["kind"] == "result" for m in out)
    if not has_result and os.path.exists(os.path.join(WEB, "demo.mp4")) and os.path.exists(os.path.join(WEB, "demo_track.json")):
        out.append(dict(id="demo", name="기본 데모 — 매빅 0314 운동장", kind="demo", kind_ko=KIND_KO["demo"],
                        created="", start="2026-10-11T15:41:55", size=os.path.getsize(os.path.join(WEB, "demo.mp4")),
                        status=dict(state="ready", progress=1, msg=""), has_track=True, has_log=False,
                        result={}, params={}, files={}))
    return out


def list_models():
    out = []
    for n in sorted(os.listdir(MODELS)):
        p = os.path.join(MODELS, n)
        if n.lower().endswith(".pt") or (os.path.isdir(p) and os.path.exists(os.path.join(p, "model.ncnn.param"))):
            out.append(n)
    out.sort(key=lambda n: (0 if "vdcont" in n.lower() else 1 if n.lower().endswith(".pt") else 2, n))
    return out


def deps():
    d = {}
    for mod in ("cv2", "ultralytics", "numpy", "pymavlink"):
        try:
            __import__(mod)
            d[mod] = True
        except Exception:
            d[mod] = False
    return d


# ================================================================ 탐지기
class Detector:
    def __init__(self, path, imgsz, conf):
        self.imgsz, self.conf = int(imgsz), float(conf)
        try:
            from ultralytics import YOLO
        except ImportError:
            raise RuntimeError("노트북에 ultralytics가 없습니다. 자료 보관함의 [필요 프로그램 설치]를 누르거나 "
                               "PowerShell에서 pip install ultralytics 를 실행하세요.")
        self.m = YOLO(path, task="detect")
        names = self.m.names if isinstance(self.m.names, dict) else dict(enumerate(self.m.names))
        self.keep = {int(i): ("person" if str(n).lower() in PERSON else "vehicle")
                     for i, n in names.items() if str(n).lower() in PERSON | VEHICLE}
        if not self.keep:
            raise RuntimeError("이 모델에는 사람·차량 클래스가 없습니다")

    def __call__(self, img):
        h, w = img.shape[:2]
        r = self.m.predict(img, imgsz=self.imgsz, conf=self.conf, classes=list(self.keep), verbose=False)[0]
        d = [(self.keep[int(c)], float(p), b[0] / w, b[1] / h, b[2] / w, b[3] / h)
             for c, p, b in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist(), r.boxes.xyxy.tolist())]
        d.sort(key=lambda x: -x[1])
        keep = []
        for x in d:      # pedestrian·people 처럼 같은 종류로 묶인 박스끼리 한 번 더 겹침 제거
            if all(x[0] != y[0] or _iou(x[2:], y[2:]) < 0.45 for y in keep):
                keep.append(x)
        return keep


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


_det_cache = {}


def get_detector(model, imgsz, conf):
    path = os.path.join(MODELS, model)
    if not os.path.exists(path):
        raise RuntimeError(f"모델 파일이 없습니다: models\\{model}")
    key = (path, int(imgsz))
    if key not in _det_cache:
        _det_cache.clear()
        _det_cache[key] = Detector(path, imgsz, conf)
    d = _det_cache[key]
    d.conf = float(conf)
    return d


# ================================================================ 비행 데이터
class Tel:
    """시간순 자세·위치 기록을 받아 임의 시각으로 보간"""

    def __init__(self, rows):
        rows = sorted(rows, key=lambda r: r["t"])
        if len(rows) < 2:
            raise RuntimeError("영상과 같이 기록된 위치 데이터가 부족합니다")
        self.r = rows
        self.t = [r["t"] for r in rows]
        off, prev, self.yaw = 0.0, None, []
        for r in rows:
            y = r["yaw"]
            if prev is not None:
                d = y - prev
                if d > 180:
                    off -= 360
                elif d < -180:
                    off += 360
            prev = y
            self.yaw.append(y + off)

    def at(self, t):
        t_ = self.t
        if t <= t_[0]:
            i = j = 0
        elif t >= t_[-1]:
            i = j = len(t_) - 1
        else:
            j = bisect.bisect_right(t_, t)
            i = j - 1
        a, b = self.r[i], self.r[j]
        f = 0.0 if i == j else (t - a["t"]) / (b["t"] - a["t"])
        L = lambda k: a[k] + (b[k] - a[k]) * f
        k0, k1 = max(0, i - 3), min(len(t_) - 1, j + 3)
        dt = t_[k1] - t_[k0]
        sp = 0.0
        if dt > 0:
            p, q = self.r[k0], self.r[k1]
            dn = (q["lat"] - p["lat"]) * 111320
            de = (q["lon"] - p["lon"]) * 111320 * math.cos(math.radians(p["lat"]))
            sp = math.hypot(dn, de) / dt
        return dict(lat=L("lat"), lon=L("lon"), alt=L("alt"), roll=L("roll"), pitch=L("pitch"),
                    yaw=(self.yaw[i] + (self.yaw[j] - self.yaw[i]) * f) % 360, gp=L("gp"), speed=sp,
                    mode=a.get("mode", ""))


def read_k380_csv(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            try:
                lat, lon, alt = float(r["lat"]), float(r["lon"]), float(r["alt_rel_m"])
            except (ValueError, KeyError, TypeError):
                continue
            g = lambda k: float(r[k]) if r.get(k) not in (None, "") else 0.0
            pitch = g("pitch_deg")
            pf = None
            if r.get("temp_c") not in (None, "") or r.get("fps") not in (None, ""):   # detector v3.4 이후 녹화
                num = lambda k: float(r[k]) if r.get(k) not in (None, "") else None
                pf = [num("fps"), num("infer_ms"), num("temp_c"), num("cpu_pct"), r.get("throttled") or None,
                      num("exp_us"), num("gain"), num("sharp")]
            rows.append(dict(t=float(r["t_s"]), lat=lat, lon=lon, alt=alt, roll=g("roll_deg"), pitch=pitch,
                             yaw=g("yaw_deg"), gp=-90.0 + pitch, mode=r.get("mode", ""), pf=pf))
    return rows


def read_mavic(path):
    return [dict(t=r["t"], lat=r["lat"], lon=r["lon"], alt=r["alt"], roll=r["droll"], pitch=r["dpitch"],
                 yaw=r["dyaw"], gp=r["gpitch"], mode="") for r in djimeta.read_dji_track(path)]


def ray_ground(u, v, alt, gp_deg, yaw_deg, hfov, vfov):
    """짐벌 카메라(매빅): 짐벌 피치·기체 방위로 광선-지면 교점 → (북, 동) m"""
    a, psi = math.radians(-gp_deg), math.radians(yaw_deg)
    h = (math.cos(psi), math.sin(psi), 0.0)
    r = (-math.sin(psi), math.cos(psi), 0.0)
    f = (math.cos(a) * h[0], math.cos(a) * h[1], math.sin(a))
    ud = (-math.sin(a) * h[0], -math.sin(a) * h[1], math.cos(a))
    xn = (u - 0.5) * 2 * math.tan(math.radians(hfov) / 2)
    yn = (v - 0.5) * 2 * math.tan(math.radians(vfov) / 2)
    R = [f[i] + xn * r[i] + yn * ud[i] for i in range(3)]
    if R[2] <= 1e-6:
        return None
    k = alt / R[2]
    return k * R[0], k * R[1]


def _vfov(hfov, w, h):
    return math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * h / w))


# ================================================================ 영상 분석
def process_video(m, params, progress, cancel):
    import cv2
    d = os.path.join(LIB, m["id"])
    vpath = os.path.join(d, m["files"]["video"])
    progress(0.01, "비행 데이터 읽는 중…")
    if m["kind"] == "k380":
        tel = Tel(read_k380_csv(os.path.join(d, m["files"]["csv"])))
    else:
        tel = Tel(read_mavic(vpath))
    progress(0.02, "모델 불러오는 중…")
    det = get_detector(params["model"], params["imgsz"], params["conf"])

    cap = cv2.VideoCapture(vpath)
    if not cap.isOpened():
        raise RuntimeError("영상을 열 수 없습니다 (OpenCV)")
    fps_v = cap.get(cv2.CAP_PROP_FPS) or 30.0
    nfr = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    hfov = float(params["hfov"])
    vfov = float(params.get("vfov") or 0) or _vfov(hfov, W, H)
    cam = Camera(hfov, vfov, int(params.get("cam_rot", 0)))
    step = 1.0 / float(params["fps"])
    lat0 = tel.r[0]["lat"]
    mE = 111320 * math.cos(math.radians(lat0))

    dets, crops = [], []
    idx, next_t, t, t0 = 0, 0.0, 0.0, time.time()
    while not cancel.is_set():
        if not cap.grab():
            break
        pm = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        t = pm if (pm > 0 or idx == 0) else idx / fps_v
        idx += 1
        if t + 1e-6 < next_t:
            continue
        next_t += step
        while next_t <= t:
            next_t += step
        ok, img = cap.retrieve()
        if not ok:
            continue
        st = tel.at(t)
        for cls, conf, x1, y1, x2, y2 in det(img):
            u, v = (x1 + x2) / 2, (y1 + y2) / 2
            la = lo = err = None
            if st["alt"] > 2:
                if m["kind"] == "k380":
                    g = pixel_to_geo(u, v, st["lat"], st["lon"], st["alt"], st["roll"], st["pitch"], st["yaw"], cam)
                    if g:
                        la, lo, err = g
                else:
                    g = ray_ground(u, v, st["alt"], st["gp"], st["yaw"], hfov, vfov)
                    if g:
                        n, e = g
                        rng = math.hypot(n, e)
                        slant = math.sqrt(rng ** 2 + st["alt"] ** 2)
                        err = math.sqrt(1.5 ** 2 + (slant * math.radians(1.0)) ** 2 + (rng * math.radians(2.0)) ** 2
                                        + (slant * math.radians(hfov) / W * 20) ** 2)
                        la, lo = st["lat"] + n / 111320, st["lon"] + e / mE
            dets.append(dict(t=round(t, 3), cls=cls, conf=round(conf, 3), lat=la, lon=lo,
                             err=None if err is None else round(err, 1),
                             box=[round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]))
            crops.append(_crop(img, x1, y1, x2, y2))
        if nfr:
            el = time.time() - t0
            frac = min(1.0, idx / nfr)
            eta = el / frac * (1 - frac) if frac > 0.02 else None
            progress(0.03 + 0.92 * frac, f"탐지 중 {int(frac * 100)}%" + (f" · 남은 시간 약 {int(eta // 60)}분 {int(eta % 60)}초" if eta else ""))
    cap.release()
    if cancel.is_set():
        raise RuntimeError("사용자가 취소함")
    dur = max(t, nfr / fps_v if nfr else t)

    progress(0.96, "객체 묶는 중…")
    objs = _cluster(dets, mE)
    snapdir = os.path.join(d, "snaps")
    shutil.rmtree(snapdir, ignore_errors=True)
    os.makedirs(snapdir, exist_ok=True)
    objmeta = {}
    for o in objs:
        best = max(o["idx"], key=lambda k: dets[k]["conf"])
        snap = None
        if crops[best] is not None:
            with open(os.path.join(snapdir, o["id"] + ".jpg"), "wb") as f:
                f.write(crops[best])
            snap = f"/lib/{m['id']}/snaps/{o['id']}.jpg"
        objmeta[o["id"]] = dict(n=len(o["idx"]), cep50=o["cep50"], cep90=o["cep90"], snap=snap)

    track = []
    k = 0.0
    while k <= dur + 1e-6:
        s = tel.at(k)
        track.append([round(k, 2), round(s["lat"], 7), round(s["lon"], 7), round(s["alt"], 2), round(s["yaw"], 1),
                      round(s["gp"], 1), round(s["roll"], 1), round(s["pitch"], 1), round(s["speed"], 2), s["mode"]])
        k += 0.25
    # 파이 성능 기록 (detector v3.4 이후 녹화만) — 1초 간격
    perf, last_s = [], None
    for r in getattr(tel, "r", []):
        if not r.get("pf"):
            continue
        s_ = int(r["t"])
        if s_ != last_s:
            perf.append([round(r["t"], 2)] + r["pf"])
            last_s = s_
    nP = sum(1 for o in objs if o["cls"] == "person")
    D = dict(source=m["files"]["video"], kind=m["kind"], name=m["name"],
             model=f"{params['model']} · {params['imgsz']} · 기준 {params['conf']}",
             start=m.get("start") or datetime.fromtimestamp(os.path.getmtime(vpath) - dur).isoformat(timespec="seconds"),
             hfov=round(hfov, 2), vfov=round(vfov, 2), video_w=W, video_h=H, det_fps=params["fps"],
             fields=["t", "lat", "lon", "alt", "yaw", "gimbal_pitch", "roll", "pitch", "speed", "mode"], track=track,
             det_fields=["t", "cls", "conf", "lat", "lon", "err", "obj", "box"],
             dets=[[x["t"], "p" if x["cls"] == "person" else "v", x["conf"], x["lat"], x["lon"], x["err"], x.get("obj"), x["box"]]
                   for x in dets],
             objs=objmeta, overlay=True,
             perf_fields=["t", "fps", "infer_ms", "temp_c", "cpu_pct", "throttled", "exp_us", "gain", "sharp"] if perf else None,
             perf=perf or None,
             note=f"{KIND_KO[m['kind']]} · {params['model']} 재분석 ({params['fps']} fps)")
    with open(os.path.join(d, "track.json"), "w", encoding="utf-8") as f:
        json.dump(D, f, ensure_ascii=False, separators=(",", ":"))
    return dict(duration=round(dur, 1), persons=nP, vehicles=len(objs) - nP, detections=len(dets),
                frames=idx, video=f"{W}×{H}", took=round(time.time() - t0), model=params["model"])


def _crop(img, x1, y1, x2, y2, side_px=160):
    import cv2
    h, w = img.shape[:2]
    cx, cy = (x1 + x2) / 2 * w, (y1 + y2) / 2 * h
    s = max((x2 - x1) * w, (y2 - y1) * h) * 1.8 + 24
    a, b = int(max(0, cx - s / 2)), int(max(0, cy - s / 2))
    c, e = int(min(w, cx + s / 2)), int(min(h, cy + s / 2))
    cr = img[b:e, a:c]
    if cr.size == 0:
        return None
    k = side_px / max(cr.shape[:2])
    if k < 1:
        cr = cv2.resize(cr, (int(cr.shape[1] * k), int(cr.shape[0] * k)), interpolation=cv2.INTER_AREA)
    ok, jpg = cv2.imencode(".jpg", cr, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return jpg.tobytes() if ok else None


def _cluster(dets, mE, min_hits=2):
    """탐지 → 객체 묶기 (프레임 단위 1:1 배정)
    같은 프레임에서 동시에 잡힌 박스는 서로 다른 객체로 본다 → 모여 있는 3명이 한 객체로 합쳐지지 않음.
    각 객체의 '최근 위치'(마지막 3회 평균)와 가까운 순서로 짝을 지어 준다."""
    RAD = {"person": 4.0, "vehicle": 5.0}
    frames = {}
    for k, d in enumerate(dets):
        if d["lat"] is not None:
            frames.setdefault(d["t"], []).append(k)
    objs = []

    def recent(o):
        r = o["idx"][-3:]
        return sum(dets[k]["lat"] for k in r) / len(r), sum(dets[k]["lon"] for k in r) / len(r)

    for t in sorted(frames):
        ks = frames[t]
        pairs = []
        for k in ks:
            d = dets[k]
            for j, o in enumerate(objs):
                if o["cls"] != d["cls"]:
                    continue
                la, lo = o["rec"]
                dist = math.hypot((d["lat"] - la) * 111320, (d["lon"] - lo) * mE)
                if dist < RAD[d["cls"]]:
                    pairs.append((dist, k, j))
        pairs.sort()
        used_k, used_j = set(), set()
        for dist, k, j in pairs:
            if k in used_k or j in used_j:
                continue
            used_k.add(k)
            used_j.add(j)
            objs[j]["idx"].append(k)
            objs[j]["rec"] = recent(objs[j])
        for k in ks:
            if k not in used_k:
                o = dict(cls=dets[k]["cls"], idx=[k])
                o["rec"] = recent(o)
                objs.append(o)
    objs = [o for o in objs if len(o["idx"]) >= min_hits]
    objs.sort(key=lambda o: dets[o["idx"][0]]["t"])
    cnt = {"person": 0, "vehicle": 0}
    for o in objs:
        cnt[o["cls"]] += 1
        o["id"] = ("P" if o["cls"] == "person" else "V") + "-%03d" % cnt[o["cls"]]
        o["lat"] = sum(dets[k]["lat"] for k in o["idx"]) / len(o["idx"])
        o["lon"] = sum(dets[k]["lon"] for k in o["idx"]) / len(o["idx"])
        r = sorted(math.hypot((dets[k]["lat"] - o["lat"]) * 111320, (dets[k]["lon"] - o["lon"]) * mE) for k in o["idx"])
        o["cep50"] = round(r[len(r) // 2], 2)
        o["cep90"] = round(r[min(len(r) - 1, int(len(r) * 0.9))], 2)
        for k in o["idx"]:
            dets[k]["obj"] = o["id"]
    return objs


# ================================================================ 비행 로그 분석
def _pct(a, p):
    if not a:
        return None
    a = sorted(a)
    return a[min(len(a) - 1, int(len(a) * p / 100))]


def _mean(a):
    return sum(a) / len(a) if a else None


def analyze_bin(path, progress, cancel, hfov=66.0, width_px=2304):
    try:
        from pymavlink import DFReader
    except ImportError:
        raise RuntimeError("pymavlink가 없습니다: pip install pymavlink")
    r = DFReader.DFReader_binary(path, zero_time_base=False)
    want = ["MODE", "CTUN", "GPS", "BAT", "VIBE", "RATE", "ATT", "MSG", "ERR", "EV", "ISBH", "ISBD", "PARM", "RCOU"]
    S = {k: [] for k in ("CTUN", "GPS", "BAT", "VIBE", "RATE", "ATT", "RCOU")}
    modes, msgs, errs, params = [], [], [], {}
    isbh, isbd = {}, {}
    n = 0
    gps_utc = None
    while True:
        x = r.recv_match(type=want)
        if x is None:
            break
        n += 1
        if n % 20000 == 0:
            if cancel.is_set():
                raise RuntimeError("사용자가 취소함")
            try:
                progress(0.05 + 0.75 * r.offset / max(1, r.data_len), "로그 읽는 중…")
            except Exception:
                pass
        t = x.get_type()
        ts = getattr(x, "TimeUS", 0) / 1e6
        if t == "PARM":
            params[x.Name] = x.Value
        elif t == "MODE":
            modes.append((ts, COPTER_MODES.get(int(x.ModeNum if hasattr(x, "ModeNum") else x.Mode), str(x.Mode))))
        elif t == "MSG":
            if len(msgs) < 300:
                msgs.append((ts, x.Message))
        elif t == "ERR":
            errs.append((ts, f"Subsys {x.Subsys} / ECode {x.ECode}"))
        elif t == "EV":
            pass
        elif t == "ISBH":
            isbh[x.N] = dict(type=x.type, inst=x.instance, mul=x.mul or 1, rate=getattr(x, "smp_rate", 0) or 1000)
        elif t == "ISBD":
            isbd.setdefault(x.N, []).append((x.seqno, list(x.x), list(x.y), list(x.z)))
        elif t == "CTUN":
            S["CTUN"].append((ts, x.Alt, getattr(x, "ThO", 0), getattr(x, "CRt", 0)))
        elif t == "GPS":
            if getattr(x, "I", 0) == 0:
                S["GPS"].append((ts, x.Spd, x.NSats, getattr(x, "HDop", 0), x.Status))
                if gps_utc is None and x.Status >= 3 and getattr(x, "GWk", 0):
                    utc = datetime(1980, 1, 6, tzinfo=timezone.utc) + timedelta(weeks=x.GWk, milliseconds=x.GMS) - timedelta(seconds=18)
                    gps_utc = (ts, utc.timestamp())
        elif t == "BAT":
            if getattr(x, "Inst", 0) == 0:
                S["BAT"].append((ts, x.Volt, x.Curr, getattr(x, "CurrTot", 0)))
        elif t == "VIBE":
            if getattr(x, "IMU", 0) == 0:
                S["VIBE"].append((ts, x.VibeX, x.VibeY, x.VibeZ, x.Clip))
        elif t == "RATE":
            S["RATE"].append((ts, x.R, x.P, x.Y))
        elif t == "ATT":
            S["ATT"].append((ts, x.Roll, x.Pitch, x.Yaw, x.DesRoll, x.DesPitch))
        elif t == "RCOU":
            S["RCOU"].append((ts, x.C1, x.C2, x.C3, x.C4))
    progress(0.82, "비행 구간 계산 중…")
    ct = S["CTUN"]
    if not ct:
        raise RuntimeError("CTUN(고도) 기록이 없는 로그입니다")
    T0 = ct[0][0]
    wall = (lambda ts: datetime.fromtimestamp(gps_utc[1] + ts - gps_utc[0]).strftime("%Y-%m-%d %H:%M:%S")) if gps_utc else (lambda ts: None)

    # 공중 구간
    air = [c[0] for c in ct if c[1] > 1.0 and c[2] > 0.05]
    segs = []
    if air:
        s = p = air[0]
        for tt in air[1:]:
            if tt - p > 3:
                segs.append((s, p))
                s = tt
            p = tt
        segs.append((s, p))
    segs = [g for g in segs if g[1] - g[0] >= 5]

    def within(rows, a, b):
        return [r_ for r_ in rows if a <= r_[0] <= b]

    def mode_at(ts):
        cur = modes[0][1] if modes else "-"
        for mt, mn in modes:
            if mt <= ts:
                cur = mn
            else:
                break
        return cur

    seg_out = []
    for i, (a, b) in enumerate(segs):
        V = within(S["VIBE"], a, b)
        R = within(S["RATE"], a, b)
        C = within(ct, a, b)
        G = within(S["GPS"], a, b)
        B = within(S["BAT"], a, b)
        O = within(S["RCOU"], a, b)
        w = [math.hypot(x[1], x[2]) for x in R]
        p90 = _pct(w, 90)
        mm = []
        for mt, mn in [(a, mode_at(a))] + [mm_ for mm_ in modes if a < mm_[0] <= b]:
            if not mm or mm[-1] != mn:
                mm.append(mn)
        motors = [round(_mean([o[k] for o in O]) or 0) for k in (1, 2, 3, 4)] if O else []
        seg_out.append(dict(
            no=i + 1, t0=round(a - T0, 1), t1=round(b - T0, 1), dur=round(b - a), start=wall(a), modes=mm,
            alt_max=round(max(c[1] for c in C), 1) if C else None,
            spd_max=round(max(g[1] for g in G), 1) if G else None,
            sats_min=min(g[2] for g in G) if G else None,
            vibe_mean=[round(_mean([v[k] for v in V]), 1) for k in (1, 2, 3)] if V else None,
            vibe_max=[round(max(v[k] for v in V), 1) for k in (1, 2, 3)] if V else None,
            clip=(V[-1][4] - V[0][4]) if V else None,
            rate=[round(_pct(w, 50), 1), round(p90, 1), round(_pct(w, 99), 1)] if w else None,
            blur=[[f"1/{int(1 / e)}", round(p90 * e * width_px / hfov, 1)] for e in (1 / 60, 1 / 100, 1 / 250, 1 / 500, 1 / 1000)] if w else None,
            volt=[round(B[0][1], 2), round(B[-1][1], 2), round(min(x[1] for x in B), 2)] if B else None,
            curr_mean=round(_mean([x[2] for x in B]), 1) if B else None,
            motors=motors, motor_spread=(max(motors) - min(motors)) if motors else None,
        ))

    # 2 Hz 시계열 (그래프용)
    def series(rows, cols, hz=2.0):
        out, last = [], -1e9
        for r_ in rows:
            if r_[0] - last >= 1.0 / hz:
                out.append([round(r_[0] - T0, 2)] + [None if v is None else round(v, 3) for v in (r_[c] for c in cols)])
                last = r_[0]
        return out

    rate_mag = [(x[0], math.hypot(x[1], x[2])) for x in S["RATE"]]
    progress(0.9, "진동 스펙트럼 계산 중…")
    fft = _batch_fft(isbh, isbd)

    # 판정·권고
    adv = []
    if seg_out:
        vm = [s for s in seg_out if s["vibe_mean"]]
        if vm:
            xs = _mean([s["vibe_mean"][0] for s in vm])
            ys = _mean([s["vibe_mean"][1] for s in vm])
            zs = _mean([s["vibe_mean"][2] for s in vm])
            worst = max(xs, ys, zs)
            lvl = "좋음" if worst < 15 else "양호" if worst < 30 else "큼"
            adv.append(("good" if worst < 15 else "ok" if worst < 30 else "bad",
                        f"진동 평균 X {xs:.1f} / Y {ys:.1f} / Z {zs:.1f} m/s² → {lvl} (15 미만 좋음, 30 미만 양호)"))
            if min(xs, ys) > 0 and max(xs, ys) / min(xs, ys) > 1.5:
                ax = "좌우(Y)" if ys > xs else "앞뒤(X)"
                adv.append(("warn", f"{ax} 진동이 다른 축의 {max(xs, ys) / min(xs, ys):.1f}배 → 특정 프롭·모터 불균형이나 암 유격 의심 (프롭 밸런싱 권장)"))
        clip = sum(s["clip"] or 0 for s in seg_out)
        if clip > 0:
            adv.append(("bad", f"가속도계 클리핑 {clip}회 — 진동이 센서 한계를 넘었습니다. 프롭·모터·FC 방진 점검 필요"))
        rp = [s["rate"][1] for s in seg_out if s["rate"]]
        if rp:
            adv.append(("info", f"기체 회전속도 90% 값 {max(rp):.1f}°/s → 1/100초 노출이면 영상 번짐 약 {max(rp) * 0.01 * width_px / hfov:.1f} px, "
                                f"1/1000초면 {max(rp) * 0.001 * width_px / hfov:.1f} px (원본 {width_px} px 기준)"))
        cm = [s["curr_mean"] for s in seg_out if s["curr_mean"] is not None]
        if cm and max(cm) < 3:
            adv.append(("warn", f"비행 중 평균 전류가 {max(cm):.1f} A로 너무 작음 → 전류 센서 보정(BATT_AMP_PERVLT) 또는 배선 확인. 배터리 %를 믿지 말고 전압을 보세요"))
        ms = [s["motor_spread"] for s in seg_out if s["motor_spread"] is not None]
        if ms and max(ms) > 120:
            adv.append(("warn", f"모터 출력 차이 최대 {max(ms)} µs → 무게중심·모터 정렬 확인 (전진 비행이 많으면 커질 수 있음)"))
    if params.get("INS_LOG_BAT_MASK", 0) == 0:
        adv.append(("info", "고속 진동 기록이 꺼져 있음 (INS_LOG_BAT_MASK=0) → Mission Planner에서 1로 바꾸고 1분 호버하면 진동 주파수를 볼 수 있습니다"))
    if fft and fft.get("peak"):
        pk = fft["peak"]
        hover = params.get("MOT_THST_HOVER", 0.25)
        if params.get("INS_HNTCH_ENABLE", 0) == 0:
            adv.append(("warn", f"진동 주파수 최고점 약 {pk:.0f} Hz. 하모닉 노치 필터 권장값: INS_HNTCH_ENABLE=1 → (재부팅) → "
                                f"INS_HNTCH_MODE=1, INS_HNTCH_REF={hover:.2f}, INS_HNTCH_FREQ={pk * 0.8:.0f}, INS_HNTCH_BW={pk * 0.4:.0f}"))
        else:
            adv.append(("info", f"진동 주파수 최고점 약 {pk:.0f} Hz (노치 필터 켜짐, 현재 FREQ={params.get('INS_HNTCH_FREQ')})"))
    if errs:
        adv.append(("bad", f"오류 기록 {len(errs)}건 — 아래 이벤트 목록 확인"))

    keep_p = {k: params[k] for k in sorted(params) if k.startswith(("INS_HNTCH", "INS_LOG_BAT", "MOT_THST_HOVER", "BATT_", "FLTMODE",
                                                                    "INS_GYRO_FILTER", "INS_ACCEL_FILTER", "ATC_RAT_RLL_P", "ATC_RAT_PIT_P", "FS_"))}
    return dict(
        file=os.path.basename(path), start=wall(T0), length=round(ct[-1][0] - T0), segments=seg_out,
        modes=[[round(a - T0, 1), mn] for a, mn in modes],
        events=[[round(a - T0, 1), "메시지", s] for a, s in msgs] + [[round(a - T0, 1), "오류", s] for a, s in errs],
        series=dict(
            alt=series(ct, [1, 3]), gps=series(S["GPS"], [1, 2, 3]), bat=series(S["BAT"], [1, 2]),
            vibe=series(S["VIBE"], [1, 2, 3, 4]), rate=series(rate_mag, [1]),
            thr=series(ct, [2]),
        ),
        fft=fft, advice=adv, params=keep_p,
    )


def _batch_fft(isbh, isbd):
    if not isbd:
        return None
    try:
        import numpy as np
    except ImportError:
        return dict(error="numpy 없음")
    specs = {}
    rate = None
    for N, rows in isbd.items():
        h = isbh.get(N)
        if not h or h["type"] != 1 or h["inst"] != 0:      # 자이로 1번만
            continue
        rows.sort()
        xyz = [np.concatenate([np.array(r_[k], dtype=float) for r_ in rows]) / h["mul"] for k in (1, 2, 3)]
        L = len(xyz[0])
        if L < 64:
            continue
        win = np.hanning(L)
        rate = h["rate"]
        mags = [np.abs(np.fft.rfft((a - a.mean()) * win)) for a in xyz]
        specs.setdefault(L, []).append(mags)
    if not specs:
        return None
    L = max(specs, key=lambda k: len(specs[k]))
    arr = np.array(specs[L])            # 배치, 축, 주파수
    avg = arr.mean(axis=0)
    freq = np.fft.rfftfreq(L, 1.0 / rate)
    tot = avg.sum(axis=0)
    band = (freq >= 30) & (freq <= rate / 2 - 5)
    peak = float(freq[band][np.argmax(tot[band])]) if band.any() else None
    step = max(1, len(freq) // 256)
    return dict(rate=rate, batches=int(arr.shape[0]), peak=peak, f=[round(float(v), 1) for v in freq[::step]],
                x=[round(float(v), 4) for v in avg[0][::step]], y=[round(float(v), 4) for v in avg[1][::step]],
                z=[round(float(v), 4) for v in avg[2][::step]])


# ================================================================ 작업 대기열
class Worker:
    def __init__(self, cfg):
        self.q = queue.Queue()
        self.cur = None
        self.cancel = threading.Event()
        self.cfg = cfg
        threading.Thread(target=self.run, daemon=True).start()

    def defaults(self, m):
        models = list_models()
        if m["kind"] == "k380":
            hf, vf, rot = self.cfg["hfov"], self.cfg["vfov"], self.cfg["cam_rot"]
        else:
            hf, vf, rot = MAVIC_HFOV, 0, 0
        return dict(model=models[0] if models else "", imgsz=640, conf=0.35, fps=4, hfov=hf, vfov=vf, cam_rot=rot)

    def submit(self, iid, params=None):
        m = load_meta(iid)
        if not m:
            return False
        if m["kind"] == "result":
            return False
        if m["kind"] in ("k380", "mavic"):
            p = self.defaults(m)
            p.update({k: v for k, v in (params or {}).items() if v not in (None, "")})
            m["params"] = p
            if not deps()["ultralytics"]:
                m["status"] = dict(state="new", progress=0, msg="영상 분석 프로그램(ultralytics) 설치 후 지상국을 다시 켜면 자동으로 분석됩니다")
                save_meta(m)
                return True
        m["status"] = dict(state="queued", progress=0, msg="대기 중")
        save_meta(m)
        self.q.put(iid)
        return True

    def stop(self, iid):
        if self.cur == iid:
            self.cancel.set()
            return True
        m = load_meta(iid)
        if m and m["status"]["state"] == "queued":
            m["status"] = dict(state="new", progress=0, msg="취소됨")
            save_meta(m)
            return True
        return False

    def run(self):
        while True:
            iid = self.q.get()
            m = load_meta(iid)
            if not m or m["status"]["state"] != "queued":
                continue
            self.cur = iid
            self.cancel.clear()
            set_status(iid, state="processing", progress=0.01, msg="시작")
            last = [0.0]

            def prog(frac, msg):
                if time.time() - last[0] > 0.7:
                    set_status(iid, progress=round(frac, 3), msg=msg)
                    last[0] = time.time()

            try:
                if m["kind"] == "log":
                    res = analyze_bin(os.path.join(LIB, iid, m["files"]["bin"]), prog, self.cancel,
                                      hfov=self.cfg["hfov"])
                    with open(os.path.join(LIB, iid, "log.json"), "w", encoding="utf-8") as f:
                        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
                    summary = dict(segments=len(res["segments"]), start=res["start"], length=res["length"],
                                   fft_peak=(res["fft"] or {}).get("peak"))
                    if res["start"] and not m.get("start"):
                        m2 = load_meta(iid)
                        m2["start"] = res["start"].replace(" ", "T")
                        save_meta(m2)
                else:
                    summary = process_video(m, m["params"], prog, self.cancel)
                with _lock:
                    m2 = load_meta(iid)
                    m2["result"] = summary
                    m2["status"] = dict(state="ready", progress=1, msg="완료", done=_now())
                    save_meta(m2)
                print(f"[lib] 분석 완료 {iid}: {summary}")
            except Exception as e:
                traceback.print_exc()
                set_status(iid, state="error", msg=str(e)[:300])
            finally:
                self.cur = None


# ================================================================ 필요 프로그램 설치
_install = dict(state="idle", log="")


def _pip_install():
    """윈도우 Anaconda에서 검증된 조합: numpy 1.26 + OpenCV 4.10 + torch 2.8 CPU (torch 2.9 이상은 c10.dll 오류)"""
    _install.update(state="running", log="설치 시작 (수 분 걸릴 수 있습니다)…\n")
    steps = [
        [sys.executable, "-m", "pip", "install", "ultralytics", "numpy<2", "opencv-python==4.10.0.84"],
        [sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall", "torch==2.8.0", "torchvision==0.23.0",
         "--index-url", "https://download.pytorch.org/whl/cpu"],
    ]
    try:
        for cmd in steps:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in iter(p.stdout.readline, b""):
                _install["log"] = (_install["log"] + line.decode("utf-8", "replace"))[-4000:]
            if p.wait() != 0:
                _install["state"] = "error"
                return
        _install["state"] = "done"
    except Exception as e:
        _install.update(state="error", log=_install["log"] + str(e))


# ================================================================ 웹 API
def setup(app, args):
    worker = Worker(dict(hfov=args.hfov, vfov=args.vfov, cam_rot=args.cam_rot))
    # 시작 시: 남아 있는 분석 대기·중단 항목 정리, 새 파일 자동 분석
    for m in list_items():
        if m["kind"] != "demo" and m["status"]["state"] in ("queued", "processing"):
            set_status(m["id"], state="new", progress=0, msg="중단됨 — 다시 분석하세요")
    # 분석 프로그램이 생겼으면, 아직 분석 안 된 영상은 자동으로 분석
    if deps()["ultralytics"]:
        for m in list_items():
            if m["kind"] in ("k380", "mavic") and not m.get("has_track") and m["status"]["state"] == "new" \
                    and "ultralytics" in (m["status"].get("msg") or ""):
                worker.submit(m["id"])

    def autoqueue(ids):
        for iid in ids:
            worker.submit(iid)

    async def lib_list(request):
        new, pending = await asyncio.get_running_loop().run_in_executor(None, scan)
        autoqueue(new)
        return web.json_response(dict(items=list_items(), pending=pending, models=list_models(), deps=deps(),
                                      busy=worker.cur, folder=LIB, models_folder=MODELS, install=_install))

    async def lib_upload(request):
        reader = await request.multipart()
        saved = []
        while True:
            part = await reader.next()
            if part is None:
                break
            if not part.filename:
                continue
            name = os.path.basename(part.filename).replace("\\", "_")
            tmp = os.path.join(LIB, name + ".part")
            with open(tmp, "wb") as f:
                while True:
                    chunk = await part.read_chunk(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            dst = os.path.join(LIB, name)
            if os.path.exists(dst):
                stem, ext = os.path.splitext(name)
                dst = os.path.join(LIB, f"{stem}_{int(time.time())}{ext}")
            os.replace(tmp, dst)
            _trusted.add(os.path.basename(dst))
            os.utime(dst, (time.time() - 10, time.time() - 10))
            saved.append(os.path.basename(dst))
        new, pending = await asyncio.get_running_loop().run_in_executor(None, scan)
        autoqueue(new)
        return web.json_response(dict(saved=saved, new=new, pending=pending))

    async def lib_process(request):
        iid = request.match_info["iid"]
        try:
            params = await request.json()
        except Exception:
            params = {}
        for k in ("imgsz", "fps", "cam_rot"):
            if k in params and params[k] not in (None, ""):
                params[k] = int(params[k])
        for k in ("conf", "hfov", "vfov"):
            if k in params and params[k] not in (None, ""):
                params[k] = float(params[k])
        if not worker.submit(iid, params):
            return web.json_response({"error": "항목 없음"}, status=404)
        return web.json_response({"ok": True})

    async def lib_cancel(request):
        return web.json_response({"ok": worker.stop(request.match_info["iid"])})

    def _item_file(iid, role):
        if iid == "demo":
            return os.path.join(WEB, {"video": "demo.mp4", "track": "demo_track.json"}[role])
        m = load_meta(iid)
        if not m:
            raise web.HTTPNotFound()
        d = os.path.join(LIB, iid)
        if role == "video":
            return os.path.join(d, m["files"]["video"])
        return os.path.join(d, {"track": "track.json", "log": "log.json"}[role])

    async def lib_video(request):
        p = _item_file(request.match_info["iid"], "video")
        if not os.path.exists(p):
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Content-Type": "video/mp4", "Cache-Control": "no-cache"})

    async def lib_json(request):
        role = request.match_info["role"]
        if role not in ("track", "log"):
            raise web.HTTPNotFound()
        p = _item_file(request.match_info["iid"], role)
        if not os.path.exists(p):
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Content-Type": "application/json", "Cache-Control": "no-cache"})

    async def lib_snap(request):
        iid, name = request.match_info["iid"], os.path.basename(request.match_info["name"])
        p = os.path.join(LIB, _safe(iid), "snaps", name)
        if not os.path.exists(p):
            raise web.HTTPNotFound()
        return web.FileResponse(p)

    async def lib_open(request):
        try:
            os.startfile(LIB)       # Windows 탐색기
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": f"폴더 열기 실패: {e}"}, status=500)

    async def lib_install(request):
        if _install["state"] != "running":
            threading.Thread(target=_pip_install, daemon=True).start()
        return web.json_response({"ok": True})

    r = app.router
    r.add_get("/api/lib", lib_list)
    r.add_post("/api/lib/upload", lib_upload)
    r.add_post("/api/lib/{iid}/process", lib_process)
    r.add_post("/api/lib/{iid}/cancel", lib_cancel)
    r.add_post("/api/lib/open-folder", lib_open)
    r.add_post("/api/lib/install", lib_install)
    r.add_get("/lib/{iid}/video", lib_video)
    r.add_get("/lib/{iid}/snaps/{name}", lib_snap)
    r.add_get("/lib/{iid}/{role}.json", lib_json)
    print(f"[lib]  자료 보관함 {LIB}  (모델 {len(list_models())}개: {', '.join(list_models()) or '없음 — models 폴더에 .pt 넣기'})")
    return worker
