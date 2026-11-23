#!/usr/bin/env python3
"""
ground_station.py — 노트북 지상국 (예비 시연 v2.2)

  [드론 KoaFC] ──코아 텔레메트리(COM)──► 이 프로그램 ──UDP 14550──► Mission Planner (선택)
  [라즈베리파이] ──핫스팟/WiFi WS(:8765)──► 이 프로그램  (탐지 박스 + 크롭 + 자세 + 성능)
  이 프로그램 ──http://localhost:5000──► 브라우저 대시보드

기능: 텔레메트리 / 위치추정 / 객체 추적 / ① 위치추정 정확도 검증 / ② 탐지 스냅샷
      ③ 시스템 성능 모니터 / ④ 감시구역(지오펜스) 경보 / ⑤ AI 정찰 보고서
      ⑥ 비행 기록 — ARM~DISARM 구간의 기체 로그·객체인식 목록을 data/flights/ 에 저장

v2.2 변경점 — 파이 UART 자세 직수신 대응
  detector_node v3가 보내는 att(프레임 시각에 보간된 자세) / pos 를 받으면 위치추정에 우선 사용한다.
  파이는 FC와 UART 직결이라 프레임과 자세를 같은 모노토닉 시계로 맞춘다. 433MHz + 네트워크
  지연을 age_ms로 되짚는 기존 경로보다 정확하다. att가 없는 구버전 파이와도 그대로 호환된다.
  --telem-from-pi 를 주면 텔레메트리 패널까지 파이에서 받아 433MHz 동글 없이 운용할 수 있다.
  (실제 비행에서는 433MHz도 함께 쓰는 것을 권장 — WiFi가 끊겨도 살아있는 링크다)

실행 예 (Windows PowerShell):
  python ground_station.py --com COM5 --baud 57600 --pi 192.168.0.22      # 실비행 (권장)
  python ground_station.py --pi 192.168.0.22 --telem-from-pi --bench-alt 10  # 지상/실내 시연
  python ground_station.py --sim-telem --sim-pi                           # 하드웨어 없이 리허설
AI 보고서:  $env:ANTHROPIC_API_KEY="sk-ant-..."  (없으면 규칙 기반 보고서로 대체)
"""
import argparse
import asyncio
import base64
import bisect
import collections
import json
import math
import os
import random
import socket
import statistics
import threading
import time
import uuid
from datetime import datetime

from aiohttp import web, ClientSession, WSMsgType

from geolocate import Camera, pixel_to_geo, distance_m, EARTH_R
import replay_lib
import report_charts

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
GPS_FIX = {0: "NO GPS", 1: "NO FIX", 2: "2D FIX", 3: "3D FIX", 4: "DGPS", 5: "RTK FLOAT", 6: "RTK FIXED"}
FIX_REV = {v: k for k, v in GPS_FIX.items()}      # 파이가 보내는 문자열 → fix_type 역매핑
CLS_KO = {"person": "사람", "vehicle": "차량"}


def now_str():
    return datetime.now().strftime("%H:%M:%S")


# ============================================================ 저장소 (JSON 파일)
class Store:
    def __init__(self, data_dir):
        self.dir = data_dir
        self.snap_dir = os.path.join(data_dir, "snapshots")
        os.makedirs(self.snap_dir, exist_ok=True)
        self.lock = threading.Lock()
        self.truth = self._load("truth.json")
        self.zones = self._load("zones.json")

    def _load(self, name):
        try:
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def save(self, name):
        data = self.truth if name == "truth.json" else self.zones
        tmp = os.path.join(self.dir, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(self.dir, name))

    def clear_snaps(self):
        for fn in os.listdir(self.snap_dir):
            if fn.endswith(".jpg"):
                try:
                    os.remove(os.path.join(self.snap_dir, fn))
                except OSError:
                    pass


def point_in_poly(lat, lon, pts):
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        yi, xi = pts[i]
        yj, xj = pts[j]
        if ((yi > lat) != (yj > lat)) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


# ============================================================ 텔레메트리
class Telemetry:
    def __init__(self):
        self.lock = threading.Lock()
        self.d = dict(alt=None, speed=None, battery=None, voltage=None, fix=0, sats=0, eph=None,
                      lat=None, lon=None, roll=0.0, pitch=0.0, yaw=0.0, mode="-", armed=False,
                      last_hb=0.0)
        self.att_t = collections.deque(maxlen=400)
        self.pos_t = collections.deque(maxlen=200)
        self.msg_count = 0

    def update(self, **kw):
        with self.lock:
            self.d.update(kw)

    def snapshot(self):
        with self.lock:
            return dict(self.d)

    def add_att(self, t, r, p, y):
        with self.lock:
            self.att_t.append((t, r, p, y))
            self.d.update(roll=r, pitch=p, yaw=y)

    def add_pos(self, t, lat, lon, alt):
        with self.lock:
            self.pos_t.append((t, lat, lon, alt))
            self.d.update(lat=lat, lon=lon, alt=alt)

    def pose_at(self, t):
        """시각 t에 가장 가까운 자세/위치 (자세는 선형보간)."""
        with self.lock:
            att, pos = list(self.att_t), list(self.pos_t)
            d = dict(self.d)
        roll, pitch, yaw = d["roll"], d["pitch"], d["yaw"]
        if len(att) >= 2:
            ts = [a[0] for a in att]
            i = bisect.bisect_left(ts, t)
            if 0 < i < len(att):
                a0, a1 = att[i - 1], att[i]
                k = (t - a0[0]) / max(1e-6, a1[0] - a0[0])
                roll = a0[1] + k * (a1[1] - a0[1])
                pitch = a0[2] + k * (a1[2] - a0[2])
                yaw = a0[3] + k * ((a1[3] - a0[3] + 180) % 360 - 180)
            else:
                a = att[0] if i == 0 else att[-1]
                roll, pitch, yaw = a[1], a[2], a[3]
        lat, lon, alt = d["lat"], d["lon"], d["alt"]
        if pos:
            p = min(pos, key=lambda x: abs(x[0] - t))
            lat, lon, alt = p[1], p[2], p[3]
        return dict(lat=lat, lon=lon, alt=alt, roll=roll, pitch=pitch, yaw=yaw, eph=d["eph"], fix=d["fix"])


# ============================================================ 객체 추적 + 스냅샷 + 지오펜스
class ObjectTracker:
    def __init__(self, store: Store, min_hits=2, gate_m=6.0, max_objects=300):
        self.store, self.min_hits, self.gate_m, self.max_objects = store, min_hits, gate_m, max_objects
        self.objs = []
        self.counter = {"person": 0, "vehicle": 0}
        self.alerts = collections.deque(maxlen=200)
        self.alert_seq = 0
        self.lock = threading.Lock()
        self.mission_start = None

    def _find(self, cls, lat, lon, err):
        best, best_d = None, 1e9
        for o in self.objs:
            if o["cls"] != cls:
                continue
            d = distance_m(o["lat"], o["lon"], lat, lon)
            if d < max(self.gate_m, 1.5 * min(err, o["err"])) and d < best_d:
                best, best_d = o, d
        return best

    def _check_zones(self, o, stamp):
        if o["hits"] < self.min_hits:
            return
        for z in self.store.zones:
            if z["id"] not in o["zones"] and point_in_poly(o["lat"], o["lon"], z["points"]):
                o["zones"].add(z["id"])
                self.alert_seq += 1
                self.alerts.append(dict(seq=self.alert_seq, time=stamp, zone=z["name"], zone_id=z["id"],
                                        obj_id=o["id"], cls=o["cls"],
                                        lat=round(o["lat"], 7), lon=round(o["lon"], 7)))

    def update(self, cls, lat, lon, err, conf, now, crop_b64=None):
        with self.lock:
            if self.mission_start is None:
                self.mission_start = datetime.now()
            o = self._find(cls, lat, lon, err)
            stamp = now_str()
            dstamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")   # 목록 표시용 (날짜 포함)
            if o:
                k = 1.0 / min(o["hits"] + 1, 10)
                o["lat"] += k * (lat - o["lat"])
                o["lon"] += k * (lon - o["lon"])
                o["err"] = min(o["err"], err) * 0.7 + err * 0.3
                o["hits"] += 1
                o["conf"] = max(o["conf"], conf)
                o["last_ts"], o["last_seen"] = now, dstamp
            else:
                self.counter[cls] = self.counter.get(cls, 0) + 1
                o = dict(id=f'{"P" if cls == "person" else "V"}-{self.counter[cls]}', cls=cls,
                         lat=lat, lon=lon, err=err, conf=conf, hits=1,
                         first_seen=dstamp, last_seen=dstamp, first_ts=now, last_ts=now,
                         samples=collections.deque(maxlen=300), trail=collections.deque(maxlen=600),
                         snap_conf=0.0, snap_ver=0,
                         zones=set())
                self.objs.append(o)
                if len(self.objs) > self.max_objects:
                    self.objs.pop(0)
            o["samples"].append((lat, lon))
            o["trail"].append((round(lat, 7), round(lon, 7), dstamp))   # 탐지 위치 이동 경로 (목록 클릭 시 지도 표시)
            # ② 스냅샷: 해당 객체에서 신뢰도가 가장 높았던 순간의 크롭을 보관
            if crop_b64 and conf > o["snap_conf"] + 0.02:
                try:
                    with open(os.path.join(self.store.snap_dir, f'{o["id"]}.jpg'), "wb") as f:
                        f.write(base64.b64decode(crop_b64))
                    o["snap_conf"], o["snap_ver"] = conf, o["snap_ver"] + 1
                    o["snap_time"] = stamp
                except (OSError, ValueError):
                    pass
            # ④ 지오펜스: 확정된 객체가 구역 안에 들어오면 1회 경보
            self._check_zones(o, stamp)
            return o

    def export(self, now):
        with self.lock:
            out = []
            for o in self.objs:
                if o["hits"] < self.min_hits:
                    continue
                out.append(dict(id=o["id"], cls=o["cls"], lat=round(o["lat"], 7), lon=round(o["lon"], 7),
                                err=round(o["err"], 1), conf=round(o["conf"], 2), hits=o["hits"],
                                first_seen=o["first_seen"], last_seen=o["last_seen"],
                                active=(now - o["last_ts"]) < 5,
                                snap=f'/snap/{o["id"]}.jpg?v={o["snap_ver"]}' if o["snap_ver"] else None,
                                snap_time=o.get("snap_time"),
                                zones=sorted(o["zones"])))
            return out

    def samples_of(self, obj_id):
        with self.lock:
            for o in self.objs:
                if o["id"] == obj_id:
                    return dict(id=o["id"], cls=o["cls"], lat=o["lat"], lon=o["lon"], hits=o["hits"],
                                samples=list(o["samples"]))
        return None

    def trail_of(self, obj_id):
        with self.lock:
            for o in self.objs:
                if o["id"] == obj_id:
                    return [dict(lat=a, lon=b, time=t) for a, b, t in o.get("trail", [])]
        return None

    def all_confirmed(self):
        with self.lock:
            return [dict(id=o["id"], cls=o["cls"], lat=o["lat"], lon=o["lon"], hits=o["hits"],
                         samples=list(o["samples"])) for o in self.objs if o["hits"] >= self.min_hits]

    def recheck_zones(self):
        """구역이 새로 그려졌을 때 이미 있는 객체도 검사."""
        with self.lock:
            for o in self.objs:
                self._check_zones(o, now_str())

    def forget_zone(self, zid):
        with self.lock:
            for o in self.objs:
                o["zones"].discard(zid)

    def clear(self):
        with self.lock:
            self.objs.clear()
            self.counter = {"person": 0, "vehicle": 0}
            self.alerts.clear()
            self.mission_start = None
        self.store.clear_snaps()


# ============================================================ ① 정확도 검증
def _stats(errs):
    if not errs:
        return None
    s = sorted(errs)

    def pct(p):
        k = (len(s) - 1) * p
        f = math.floor(k)
        c = min(f + 1, len(s) - 1)
        return s[f] + (s[c] - s[f]) * (k - f)

    return dict(n=len(s), mean=round(statistics.fmean(s), 2), max=round(s[-1], 2),
                std=round(statistics.pstdev(s), 2) if len(s) > 1 else 0.0,
                rmse=round(math.sqrt(sum(e * e for e in s) / len(s)), 2),
                cep50=round(pct(0.5), 2), cep90=round(pct(0.9), 2))


def compute_accuracy(store: Store, trk: ObjectTracker, match_radius=10.0, objs=None):
    """기준점 ↔ 객체 매칭: 수동 지정(obj_id)이 우선, 나머지는 가까운 쌍부터 1:1 배정."""
    if objs is None:
        objs = trk.all_confirmed()
    by_id = {o["id"]: o for o in objs}
    assign, used = {}, set()
    for t in store.truth:
        if t.get("obj_id") and t["obj_id"] in by_id:
            assign[t["id"]] = by_id[t["obj_id"]]
            used.add(t["obj_id"])
    pairs = []
    for t in store.truth:
        if t["id"] in assign:
            continue
        for o in objs:
            if o["id"] in used or (t.get("cls") and o["cls"] != t["cls"]):
                continue
            d = distance_m(o["lat"], o["lon"], t["lat"], t["lon"])
            if d <= match_radius:
                pairs.append((d, t["id"], o))
    for d, tid, o in sorted(pairs, key=lambda x: x[0]):
        if tid in assign or o["id"] in used:
            continue
        assign[tid] = o
        used.add(o["id"])

    rows, all_est, all_samples = [], [], []
    for t in store.truth:
        target = assign.get(t["id"])
        row = dict(id=t["id"], label=t["label"], cls=t.get("cls"), lat=t["lat"], lon=t["lon"],
                   manual=bool(t.get("obj_id")), obj_id=None, est_err=None, sample_stats=None)
        if target:
            e = distance_m(target["lat"], target["lon"], t["lat"], t["lon"])
            samples = [distance_m(la, lo, t["lat"], t["lon"]) for la, lo in target["samples"]]
            dn = math.radians(target["lat"] - t["lat"]) * EARTH_R
            de = math.radians(target["lon"] - t["lon"]) * EARTH_R * math.cos(math.radians(t["lat"]))
            row.update(obj_id=target["id"], est_err=round(e, 2), sample_stats=_stats(samples),
                       dn=round(dn, 2), de=round(de, 2))
            all_est.append(e)
            all_samples += samples
        rows.append(row)
    matched = [r for r in rows if r["obj_id"]]
    bias = None
    if matched:
        bias = dict(dn=round(statistics.fmean(r["dn"] for r in matched), 2),
                    de=round(statistics.fmean(r["de"] for r in matched), 2))
    return dict(points=rows, matched=len(matched), total=len(rows), match_radius=match_radius,
                estimate=_stats(all_est), samples=_stats(all_samples), bias=bias)


# ============================================================ MAVLink (코아 텔레메트리)
def mavlink_thread(args, tel: Telemetry, stop):
    from pymavlink import mavutil

    while not stop.is_set():
        try:
            print(f"[fc] {args.com} @ {args.baud} 연결 시도…")
            m = mavutil.mavlink_connection(args.com, baud=args.baud, source_system=254,
                                           source_component=191, autoreconnect=True)
            fwd = None
            if args.mp_port:
                fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                fwd.bind(("127.0.0.1", 0))
                fwd.setblocking(False)
                print(f"[fc] Mission Planner 중계: UDP 127.0.0.1:{args.mp_port} (MP에서 UDP/{args.mp_port}로 연결)")
            requested = 0.0
            while not stop.is_set():
                if fwd:
                    try:
                        while True:
                            data, _ = fwd.recvfrom(4096)
                            m.write(data)
                    except (BlockingIOError, OSError):
                        pass
                msg = m.recv_match(blocking=True, timeout=0.05)
                if msg is None:
                    continue
                typ = msg.get_type()
                if typ == "BAD_DATA":
                    continue
                tel.msg_count += 1
                if fwd:
                    try:
                        fwd.sendto(msg.get_msgbuf(), ("127.0.0.1", args.mp_port))
                    except OSError:
                        pass
                now = time.monotonic()
                if typ == "HEARTBEAT" and msg.get_srcComponent() == 1 and msg.type != 6:
                    tel.update(mode=mavutil.mode_string_v10(msg), armed=bool(msg.base_mode & 128), last_hb=now)
                    if now - requested > 10:
                        m.mav.request_data_stream_send(msg.get_srcSystem(), 1,
                                                       mavutil.mavlink.MAV_DATA_STREAM_ALL, 5, 1)
                        m.mav.request_data_stream_send(msg.get_srcSystem(), 1,
                                                       mavutil.mavlink.MAV_DATA_STREAM_EXTRA1, 10, 1)
                        requested = now
                elif typ == "ATTITUDE":
                    tel.add_att(now, math.degrees(msg.roll), math.degrees(msg.pitch), math.degrees(msg.yaw))
                elif typ == "GLOBAL_POSITION_INT":
                    if msg.lat != 0 or msg.lon != 0:
                        tel.add_pos(now, msg.lat / 1e7, msg.lon / 1e7, msg.relative_alt / 1000.0)
                    else:
                        tel.update(alt=msg.relative_alt / 1000.0)
                elif typ == "VFR_HUD":
                    tel.update(speed=msg.groundspeed)
                elif typ == "SYS_STATUS":
                    tel.update(battery=msg.battery_remaining if msg.battery_remaining >= 0 else None,
                               voltage=msg.voltage_battery / 1000.0 if msg.voltage_battery > 0 else None)
                elif typ == "GPS_RAW_INT":
                    tel.update(fix=msg.fix_type, sats=msg.satellites_visible,
                               eph=(msg.eph / 100.0) if msg.eph not in (0, 65535) else None)
        except Exception as e:
            print(f"[fc] 오류: {e} — 3초 후 재시도")
            time.sleep(3)


def sim_telem_thread(args, tel: Telemetry, stop):
    lat0, lon0 = args.home
    t0 = time.monotonic()
    tel.update(fix=3, sats=14, eph=1.2, mode="GUIDED", armed=True, battery=88, voltage=16.1)
    while not stop.is_set():
        t = time.monotonic() - t0
        ang = t / 20.0
        n, e = 25 * math.cos(ang), 25 * math.sin(ang)
        lat = lat0 + math.degrees(n / EARTH_R)
        lon = lon0 + math.degrees(e / (EARTH_R * math.cos(math.radians(lat0))))
        now = time.monotonic()
        tel.add_pos(now, lat, lon, 20 + 0.5 * math.sin(t / 5))
        tel.add_att(now, 3 * math.sin(t / 2), -4 + math.sin(t / 3), (math.degrees(ang) + 270) % 360 - 180)
        tel.update(speed=25 / 20.0, battery=max(20, 88 - int(t / 30)), last_hb=now)
        tel.msg_count += 3
        time.sleep(0.1)


# ============================================================ 파이 탐지 수신
class Vision:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = None
        self.connected = False
        self.last_rx = 0.0
        self.msg_count = 0
        self.rtt_ms = None
        self.sys = {}
        self.att_src = None          # "pi_uart" | "ground_telem" — 위치추정에 쓴 자세 출처


SIM_TARGETS = [("person", "사람 A", 8, 5), ("person", "사람 B", -12, 10), ("vehicle", "차량 A", 15, -18),
               ("vehicle", "차량 B", -20, -6), ("person", "사람 C", 22, 20)]


def ne_to_ll(home, n, e):
    return (home[0] + math.degrees(n / EARTH_R),
            home[1] + math.degrees(e / (EARTH_R * math.cos(math.radians(home[0])))))


def sim_crop(cls, label):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    img = np.full((120, 120, 3), (70, 95, 80), np.uint8)
    img[:] = img + np.random.randint(0, 25, img.shape, dtype=np.uint8)
    if cls == "person":
        cv2.circle(img, (60, 45), 10, (40, 40, 200), -1)
        cv2.rectangle(img, (52, 55), (68, 85), (40, 40, 200), -1)
    else:
        cv2.rectangle(img, (30, 40), (90, 80), (200, 140, 40), -1)
        cv2.rectangle(img, (40, 48), (80, 60), (60, 60, 60), -1)
    cv2.putText(img, "SIM", (4, 114), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
    ok, jpg = cv2.imencode(".jpg", img)
    return base64.b64encode(jpg.tobytes()).decode() if ok else None


async def pi_client(args, vis: Vision, tel: Telemetry, trk: ObjectTracker, cam: Camera, sim=False, flog=None):
    url = f"ws://{args.pi}:{args.pi_ws_port}/ws"

    def handle(data):
        now = time.monotonic()
        rtt = vis.rtt_ms or 0

        # ── 파이가 프레임 시각에 맞춰 보간한 자세를 보내오면 그것을 쓴다 (detector_node v3)
        #    파이는 FC와 UART 직결이라 프레임과 자세를 같은 모노토닉 시계로 맞춘다.
        #    433MHz + 네트워크 지연을 age_ms로 되짚는 기존 경로보다 정확하다.
        #    att가 없는 구버전 파이면 기존 경로로 자동 폴백한다.
        att = data.get("att")
        pos = data.get("pos") or {}
        vis.att_src = "pi_uart" if att else "ground_telem"

        if att:
            if args.telem_from_pi:
                # 텔레메트리 패널까지 파이에서 받는다 (433MHz 동글 없이 운용)
                tel.add_att(now, att["roll"], att["pitch"], att["yaw"])
                if pos.get("lat"):
                    tel.add_pos(now, pos["lat"], pos["lon"], pos.get("alt_rel"))
                elif pos.get("alt_rel") is not None:
                    tel.update(alt=pos["alt_rel"])
                tel.update(speed=pos.get("speed"), battery=pos.get("battery"),
                           mode=pos.get("mode") or "-",
                           fix=FIX_REV.get(pos.get("gps"), 0), last_hb=now)
                if "armed" in pos:          # detector_node v3.1+ — ARM 상태로 비행 기록 자동 시작/종료
                    tel.update(armed=bool(pos["armed"]))
                if pos.get("sats") is not None:
                    tel.update(sats=pos["sats"])
                tel.msg_count += 1
            t = tel.snapshot()
            pose = dict(lat=pos.get("lat") or None,      # GPS 미고정이면 파이가 0.0을 보낸다
                        lon=pos.get("lon") or None,
                        alt=pos.get("alt_rel"),
                        roll=att["roll"], pitch=att["pitch"], yaw=att["yaw"],
                        eph=t["eph"], fix=t["fix"])
        else:
            t_cap = now - (data.get("age_ms", 0) + (rtt / 2 if vis.rtt_ms else args.net_delay_ms)) / 1000.0
            pose = tel.pose_at(t_cap)

        alt = pose["alt"]
        geo_mode = "estimated"
        if args.bench_alt and (alt is None or alt < 1.0):
            alt, geo_mode = args.bench_alt, "bench"
        lat, lon = pose["lat"], pose["lon"]
        if lat is None and args.home_fallback:
            lat, lon, geo_mode = args.home[0], args.home[1], "no-gps"
        boxes = []
        for b in data.get("boxes", []):
            crop = b.pop("crop", None)
            b["lat"] = b["lon"] = b["geo_conf"] = None
            if lat is not None and alt is not None:
                u, v = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2   # 수직하방 카메라 → 박스 중심
                r = pixel_to_geo(u, v, lat, lon, alt, pose["roll"], pose["pitch"], pose["yaw"], cam,
                                 gps_err_m=pose["eph"] or 2.5)
                if r:
                    b["lat"], b["lon"], b["geo_err_m"] = round(r[0], 7), round(r[1], 7), round(r[2], 1)
                    b["geo_conf"] = geo_mode
                    b["obj_id"] = trk.update(b["cls"], r[0], r[1], r[2], b["conf"], now, crop)["id"]
            if flog is not None:
                flog.add_det(b, pose if lat is None else dict(pose, lat=lat, lon=lon), alt, geo_mode)
            boxes.append(b)
        data["boxes"] = boxes
        with vis.lock:
            vis.last = data
            vis.last_rx = now
            vis.msg_count += 1
            if data.get("sys"):
                vis.sys = data["sys"]

    if sim:
        seq = 0
        vis.connected = True
        vis.rtt_ms = 45.0
        crops = {lab: sim_crop(c, lab) for c, lab, _, _ in SIM_TARGETS}
        t0 = time.monotonic()
        while True:
            await asyncio.sleep(0.2)
            seq += 1
            pose = tel.pose_at(time.monotonic())
            if pose["lat"] is None:
                continue
            boxes = []
            hf, vf = math.radians(cam.hfov_deg / 2), math.radians(cam.vfov_deg / 2)
            yaw = math.radians(pose["yaw"])
            dn0 = math.radians(pose["lat"] - args.home[0]) * EARTH_R
            de0 = math.radians(pose["lon"] - args.home[1]) * EARTH_R * math.cos(math.radians(args.home[0]))
            for cls, lab, tn, te in SIM_TARGETS:
                dn, de = tn - dn0, te - de0
                fwd = dn * math.cos(yaw) + de * math.sin(yaw)
                right = -dn * math.sin(yaw) + de * math.cos(yaw)
                u = 0.5 + right / (pose["alt"] * math.tan(hf)) / 2
                v = 0.5 - fwd / (pose["alt"] * math.tan(vf)) / 2
                if 0.03 < u < 0.97 and 0.03 < v < 0.97 and random.random() > 0.15:
                    w, h = (0.03, 0.05) if cls == "person" else (0.08, 0.06)
                    boxes.append(dict(cls=cls, conf=round(random.uniform(0.55, 0.9), 2),
                                      x=u - w / 2 + random.gauss(0, 0.004), y=v - h / 2 + random.gauss(0, 0.004),
                                      w=w, h=h, crop=crops[lab]))
            t = time.monotonic() - t0
            fps = round(6 + math.sin(t / 7) + random.uniform(-0.4, 0.4), 1)
            handle(dict(type="detections", seq=seq, fps=fps, infer_ms=round(1000 / 7 + random.uniform(-8, 8), 1),
                        age_ms=round(150 + random.uniform(-20, 30), 1), boxes=boxes,
                        sys=dict(temp_c=round(58 + t / 20 % 12, 1), throttled="0x0",
                                 cpu_pct=round(70 + random.uniform(-6, 6), 1))))

    async def pinger(ws):
        pending = {}
        while not ws.closed:
            pid = uuid.uuid4().hex[:8]
            pending[pid] = time.monotonic()
            try:
                await ws.send_str(json.dumps({"type": "ping", "id": pid}))
            except Exception:
                return
            await asyncio.sleep(2)
            for k in [k for k, v in pending.items() if time.monotonic() - v > 10]:
                pending.pop(k, None)
            vis._pending = pending

    while True:
        try:
            async with ClientSession() as s:
                print(f"[pi] {url} 연결 시도…")
                async with s.ws_connect(url, heartbeat=10, timeout=5) as ws:
                    vis.connected = True
                    vis._pending = {}
                    print("[pi] 연결됨")
                    ptask = asyncio.create_task(pinger(ws))
                    async for msg in ws:
                        if msg.type == WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("type") == "pong":
                                t_sent = getattr(vis, "_pending", {}).pop(data.get("id"), None)
                                if t_sent:
                                    r = (time.monotonic() - t_sent) * 1000
                                    vis.rtt_ms = r if vis.rtt_ms is None else vis.rtt_ms * 0.7 + r * 0.3
                                continue
                            handle(data)
                        elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                            break
                    ptask.cancel()
        except Exception as e:
            print(f"[pi] 연결 실패: {e.__class__.__name__} {e}")
        vis.connected = False
        await asyncio.sleep(2)


# ============================================================ ③ 성능 기록
class PerfLog:
    def __init__(self, seconds=300):
        self.hist = collections.deque(maxlen=seconds)
        self._fc = self._pi = 0
        self._t = time.monotonic()

    def sample(self, tel: Telemetry, vis: Vision):
        now = time.monotonic()
        dt = max(1e-3, now - self._t)
        fc_rate = (tel.msg_count - self._fc) / dt
        pi_rate = (vis.msg_count - self._pi) / dt
        self._fc, self._pi, self._t = tel.msg_count, vis.msg_count, now
        with vis.lock:
            det = vis.last
            stale = now - vis.last_rx > 2.0
            sysd = dict(vis.sys)
        rtt = vis.rtt_ms
        age = det.get("age_ms") if det and not stale else None
        s = dict(t=round(time.time(), 1),
                 fps=det.get("fps") if det and not stale else None,
                 infer_ms=det.get("infer_ms") if det and not stale else None,
                 e2e_ms=round(age + rtt / 2, 1) if (age is not None and rtt is not None) else age,
                 rtt_ms=round(rtt, 1) if rtt is not None else None,
                 temp_c=sysd.get("temp_c"), cpu_pct=sysd.get("cpu_pct"), throttled=sysd.get("throttled"),
                 fc_rate=round(fc_rate, 1), pi_rate=round(pi_rate, 1))
        self.hist.append(s)
        return s

    def summary(self):
        h = list(self.hist)

        def agg(key):
            v = [x[key] for x in h if x.get(key) is not None]
            return dict(avg=round(statistics.fmean(v), 1), min=min(v), max=max(v)) if v else None

        return {k: agg(k) for k in ("fps", "infer_ms", "e2e_ms", "rtt_ms", "temp_c", "cpu_pct")}


# ============================================================ ⑥ 비행 기록 (비행 시작~종료 구간의 기체 로그 + 객체인식 로그)
class FlightLog:
    """ARM → 기록 시작, DISARM → 기록 종료 후 저장. 대시보드 버튼으로 수동 시작/종료도 가능.
    FC 링크가 끊긴 동안에는 ARM 상태를 판단하지 않는다 (통신 두절을 착륙으로 오인하지 않도록)."""
    TEL_COLS = ["time", "t_s", "mode", "armed", "alt_m", "speed_ms", "battery_pct", "voltage_v",
                "gps", "sats", "lat", "lon", "heading_deg", "roll_deg", "pitch_deg"]
    DET_COLS = ["time", "t_s", "obj_id", "cls", "conf", "lat", "lon", "err_m",
                "drone_lat", "drone_lon", "drone_alt_m", "geo_mode"]
    MAX_DET = 60000

    def __init__(self, data_dir, auto=True):
        self.dir = os.path.join(data_dir, "flights")
        os.makedirs(self.dir, exist_ok=True)
        self.auto = auto
        self.lock = threading.Lock()
        self.cur = None
        self.prev_armed = None
        self.last_id = None
        self.index = {}
        for fn in sorted(os.listdir(self.dir)):
            if fn.startswith("flight_") and fn.endswith(".json"):
                try:
                    with open(os.path.join(self.dir, fn), encoding="utf-8") as f:
                        sm = json.load(f)["summary"]
                    self.index[sm["id"]] = sm
                    self.last_id = sm["id"]
                except (OSError, ValueError, KeyError):
                    pass

    # ---- 상태
    def status(self):
        with self.lock:
            c = self.cur
            return dict(recording=c is not None, auto=self.auto, last_id=self.last_id,
                        id=c["id"] if c else None,
                        elapsed=round(time.monotonic() - c["t0"]) if c else 0,
                        tel_n=len(c["tel"]) if c else 0, det_n=len(c["det"]) if c else 0)

    def start(self, reason):
        with self.lock:
            if self.cur:
                return self.cur["id"]
            now = datetime.now()
            fid = now.strftime("%Y%m%d_%H%M%S")
            self.cur = dict(id=fid, start=now, t0=time.monotonic(), reason_start=reason, tel=[], det=[])
        print(f"[flight] 기록 시작 {fid} ({reason})")
        return fid

    def stop(self, reason, trk=None):
        with self.lock:
            c, self.cur = self.cur, None
        if not c:
            return None
        end = datetime.now()
        tel, det = c["tel"], c["det"]
        alts = [r["alt_m"] for r in tel if r["alt_m"] is not None]
        spds = [r["speed_ms"] for r in tel if r["speed_ms"] is not None]
        bats = [r["battery_pct"] for r in tel if r["battery_pct"] is not None]
        dist, prev = 0.0, None
        for r in tel:
            if r["lat"] is None:
                continue
            if prev:
                dist += distance_m(prev[0], prev[1], r["lat"], r["lon"])
            prev = (r["lat"], r["lon"])
        # 객체인식 목록: 이번 비행에서 탐지된 객체만 (객체 ID 기준으로 묶음)
        objs = {}
        for d in det:
            if not d["obj_id"]:
                continue
            o = objs.get(d["obj_id"])
            if o is None:
                o = objs[d["obj_id"]] = dict(id=d["obj_id"], cls=d["cls"], conf=d["conf"], hits=0,
                                             first_seen=d["time"], last_seen=d["time"], lat=None, lon=None, err=None)
            o["hits"] += 1
            o["conf"] = max(o["conf"], d["conf"])
            o["last_seen"] = d["time"]
        if trk is not None:      # 최종 위치는 추적기가 누적 평균한 값
            for e in trk.export(time.monotonic()):
                if e["id"] in objs:
                    objs[e["id"]].update(lat=e["lat"], lon=e["lon"], err=e["err"], snap=e.get("snap"))
        obj_list = sorted(objs.values(), key=lambda o: o["id"])
        sm = dict(id=c["id"], start=c["start"].strftime("%Y-%m-%d %H:%M:%S"),
                  end=end.strftime("%Y-%m-%d %H:%M:%S"),
                  duration_s=round(time.monotonic() - c["t0"]),
                  reason_start=c["reason_start"], reason_end=reason,
                  tel_n=len(tel), det_n=len(det),
                  max_alt_m=round(max(alts), 1) if alts else None,
                  max_speed_ms=round(max(spds), 1) if spds else None,
                  dist_m=round(dist),
                  battery_start=bats[0] if bats else None, battery_end=bats[-1] if bats else None,
                  persons=sum(1 for o in obj_list if o["cls"] == "person"),
                  vehicles=sum(1 for o in obj_list if o["cls"] == "vehicle"))
        rec = dict(summary=sm, telemetry=tel, detections=det, objects=obj_list)
        base = os.path.join(self.dir, f"flight_{c['id']}")
        try:
            with open(base + ".json.tmp", "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
            os.replace(base + ".json.tmp", base + ".json")
            for name, cols, rows in (("telemetry", self.TEL_COLS, tel), ("detections", self.DET_COLS, det)):
                with open(f"{base}_{name}.csv", "w", encoding="utf-8-sig", newline="") as f:
                    f.write(",".join(cols) + "\n")
                    for r in rows:
                        f.write(",".join("" if r.get(k) is None else str(r[k]) for k in cols) + "\n")
        except OSError as e:
            print(f"[flight] 저장 실패: {e}")
        with self.lock:
            self.index[c["id"]] = sm
            self.last_id = c["id"]
        print(f"[flight] 기록 종료 {c['id']} ({reason}) — 기체 로그 {len(tel)}건, 탐지 {len(det)}건 → {base}.json")
        return sm

    # ---- 1초마다 호출: ARM 상태 감시 + 기체 로그 1행
    def tick(self, tel_snap, fc_link, trk):
        armed = bool(tel_snap["armed"])
        if self.auto and fc_link:
            if armed and not self.prev_armed:
                self.start("자동 (ARM)")
            elif not armed and self.prev_armed:
                self.stop("자동 (DISARM)", trk)
            self.prev_armed = armed
        with self.lock:
            c = self.cur
            if not c:
                return
            t = tel_snap

            def r(v, nd=1):
                return None if v is None else round(v, nd)

            c["tel"].append(dict(time=now_str(), t_s=round(time.monotonic() - c["t0"]), mode=t["mode"],
                                 armed=armed, alt_m=r(t["alt"]), speed_ms=r(t["speed"]),
                                 battery_pct=t["battery"], voltage_v=r(t["voltage"], 2),
                                 gps=GPS_FIX.get(t["fix"], "?"), sats=t["sats"],
                                 lat=r(t["lat"], 7), lon=r(t["lon"], 7),
                                 heading_deg=r(t["yaw"]), roll_deg=r(t["roll"]), pitch_deg=r(t["pitch"])))

    # ---- 탐지 1건 (파이 수신 스레드에서 호출)
    def add_det(self, b, pose, alt, geo_mode):
        with self.lock:
            c = self.cur
            if not c or len(c["det"]) >= self.MAX_DET:
                return
            c["det"].append(dict(time=now_str(), t_s=round(time.monotonic() - c["t0"], 1),
                                 obj_id=b.get("obj_id"), cls=b["cls"], conf=b["conf"],
                                 lat=b.get("lat"), lon=b.get("lon"), err_m=b.get("geo_err_m"),
                                 drone_lat=None if pose["lat"] is None else round(pose["lat"], 7),
                                 drone_lon=None if pose["lon"] is None else round(pose["lon"], 7),
                                 drone_alt_m=None if alt is None else round(alt, 1),
                                 geo_mode=b.get("geo_conf") or "no-geo"))

    def list(self):
        with self.lock:
            return sorted(self.index.values(), key=lambda s: s["id"], reverse=True)

    def get(self, fid):
        fid = os.path.basename(fid)
        try:
            with open(os.path.join(self.dir, f"flight_{fid}.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None


# ============================================================ ⑤ 정찰 보고서
def build_report_input(args, tel, trk, store, perf, vis=None):
    now = time.monotonic()
    objs = trk.export(now)
    t = tel.snapshot()
    acc = compute_accuracy(store, trk, args.match_radius)
    att_src = getattr(vis, "att_src", None) if vis else None
    return dict(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        mission_start=trk.mission_start.strftime("%Y-%m-%d %H:%M:%S") if trk.mission_start else None,
        mode="지상(벤치) 모드 — 가상 고도 사용" if args.bench_alt else ("시뮬레이션" if args.sim_pi else "실비행"),
        attitude_source=("기체 탑재 라즈베리파이 UART 직수신 (프레임 시각 동기화)"
                         if att_src == "pi_uart" else
                         "지상 텔레메트리 역추적 (age_ms 보정)" if att_src else None),
        drone=dict(lat=round(t["lat"], 6) if t["lat"] else None, lon=round(t["lon"], 6) if t["lon"] else None,
                   alt_m=round(t["alt"], 1) if t["alt"] is not None else None, battery=t["battery"], gps=GPS_FIX.get(t["fix"]),
                   sats=t["sats"], flight_mode=t["mode"]),
        counts=dict(person=sum(o["cls"] == "person" for o in objs), vehicle=sum(o["cls"] == "vehicle" for o in objs)),
        objects=[{k: o.get(k) for k in ("id", "cls", "lat", "lon", "err", "conf", "hits", "first_seen", "last_seen", "zones", "active")}
                 for o in objs],
        zones=[dict(id=z["id"], name=z["name"]) for z in store.zones],
        alerts=list(trk.alerts),
        accuracy=dict(matched=acc["matched"], total=acc["total"], estimate=acc["estimate"],
                      samples=acc["samples"], bias=acc["bias"]),
        performance=perf.summary(),
    ), objs


KIND_REPORT = {"mavic": "DJI 매빅4 프로 촬영 영상", "k380": "K380 기체 촬영 영상", "result": "모델 결과 영상"}


def _cut_info(c):
    """리플레이 보고서 기준 시점: 영상 처음 ~ 지정 시점까지만 분석했을 때의 표시용 정보"""
    try:
        t, full = float(c["t"]), float(c["full"])
    except (TypeError, KeyError, ValueError):
        return None
    ms = lambda x: f"{int(x) // 60}:{int(x) % 60:02d}"
    return dict(t=t, full=full, text=f"영상 0:00 ~ {ms(t)} (전체 {ms(full)} 중 앞 {t / max(full, 1) * 100:.0f}%)")


def build_replay_report_input(rp, store, args):
    """리플레이 중 보고서: 화면의 영상 전체(끝 시점) 결과로 작성 — 지상국 실시간/시뮬 데이터는 쓰지 않음."""
    objs = []
    for o in rp.get("objects") or []:
        try:
            objs.append(dict(id=str(o["id"]), cls=o["cls"] if o.get("cls") in CLS_KO else "person",
                             lat=float(o["lat"]), lon=float(o["lon"]), err=o.get("err"), conf=o.get("conf"),
                             hits=o.get("hits"), first_seen=str(o.get("first_seen", ""))[11:] or "-",
                             last_seen=str(o.get("last_seen", ""))[11:] or "-", zones=list(o.get("zones") or []),
                             left=bool(o.get("left")), active=bool(o.get("active")), snap=o.get("snap") or None,
                             samples=[(float(o["lat"]), float(o["lon"]))]))
        except (KeyError, TypeError, ValueError):
            continue
    acc = compute_accuracy(store, None, args.match_radius, objs=objs)
    start = str(rp.get("start") or "").replace("T", " ")[:19]
    dr = rp.get("drone") or {}
    kind = rp.get("kind") or "mavic"
    r2 = lambda v, n=6: round(float(v), n) if isinstance(v, (int, float)) else None
    alerts = []
    for a in rp.get("alerts") or []:
        try:
            alerts.append(dict(time=str(a.get("time", "")), zone=str(a.get("zone", "")), cls=a["cls"] if a.get("cls") in CLS_KO else "person",
                               obj_id=str(a.get("obj_id", "")), lat=float(a["lat"]), lon=float(a["lon"]),
                               t=float(a["t"]) if isinstance(a.get("t"), (int, float)) else None))
        except (KeyError, TypeError, ValueError):
            continue
    dur = rp.get("duration") or 0
    d = dict(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        mission_start=start or None,
        mode=f"리플레이 분석 (녹화 영상 사후 분석) — {KIND_REPORT.get(kind, kind)}",
        replay=dict(name=rp.get("name"), kind=KIND_REPORT.get(kind, kind), model=rp.get("model") or None,
                    shot_at=start or None, end_at=str(rp.get("clock_end") or "")[:19] or None,
                    length=f"{int(dur) // 60}분 {int(dur) % 60}초", alt_range_m=[rp.get("alt_min"), rp.get("alt_max")],
                    cut=_cut_info(rp.get("cut")),
                    path_m=rp.get("path_m")),
        attitude_source=("DJI 영상 메타데이터 (짐벌 피치·기체 방위)" if kind in ("mavic", "result")
                         else "비행 로그(ArduPilot) 자세 기록" if kind == "k380" else None),
        drone=dict(lat=r2(dr.get("lat")), lon=r2(dr.get("lon")), alt_m=r2(dr.get("alt_m"), 1), battery=None,
                   gps="DJI GNSS 기록" if kind in ("mavic", "result") else "비행 로그 GPS 기록", sats=None,
                   flight_mode=dr.get("mode") or None),
        counts=dict(person=sum(o["cls"] == "person" for o in objs), vehicle=sum(o["cls"] == "vehicle" for o in objs),
                    left=sum(o["left"] for o in objs)),
        objects=[{k: o[k] for k in ("id", "cls", "lat", "lon", "err", "conf", "hits", "first_seen", "last_seen", "zones", "left", "active")}
                 for o in objs],
        zones=[dict(id=z["id"], name=z["name"]) for z in store.zones],
        alerts=alerts,
        accuracy=dict(matched=acc["matched"], total=acc["total"], estimate=acc["estimate"],
                      samples=acc["samples"], bias=acc["bias"]),
        performance={},
    )
    return d, objs


def template_report(d):
    L = []
    L.append("# 정찰 결과 보고서 (자동 생성 — 규칙 기반)")
    L.append(f"- 작성 시각: {d['generated']}  /  임무 시작: {d['mission_start'] or '-'}  /  운용: {d['mode']}")
    if d.get("attitude_source"):
        L.append(f"- 자세 데이터 출처: {d['attitude_source']}")
    dr = d["drone"]
    na = lambda v, u="": "-" if v is None else f"{v}{u}"
    rp = d.get("replay")
    if rp:
        ar = rp.get("alt_range_m") or [None, None]
        if rp.get("cut"):
            L.append(f"- **분석 구간: {rp['cut']['text']} — 지정한 기준 시점({(rp['end_at'] or '-')[11:]})까지의 탐지 결과만 반영**")
        L.append(f"- 분석 영상: {rp['name']} ({rp['kind']}) · 촬영 {rp['shot_at'] or '-'} ~ {(rp['end_at'] or '-')[11:]} · "
                 + (f"분석 길이 {rp['length']}" if rp.get("cut") else f"길이 {rp['length']}"))
        L.append(f"- 탐지 모델: {rp['model'] or '-'} · 비행 고도 {na(ar[0])}~{na(ar[1], ' m')} · 이동 거리 약 {na(rp.get('path_m'), ' m')}")
        L.append(f"- 드론 최종 위치({'기준 시점' if rp.get('cut') else '영상 끝'}): {na(dr['lat'])}, {na(dr['lon'])} · 고도 {na(dr['alt_m'], ' m')} · 위치 출처 {dr['gps']}")
    else:
        L.append(f"- 드론 최종 위치: {na(dr['lat'])}, {na(dr['lon'])} · 고도 {na(dr['alt_m'], ' m')} · "
                 f"배터리 {na(dr['battery'], '%')} · GPS {dr['gps']}({dr['sats']})")
    cv = d.get("coverage")
    if cv and cv.get("area_m2"):
        ar = f"{cv['area_m2'] / 10000:.2f} ha" if cv["area_m2"] >= 10000 else f"{cv['area_m2']:,} m²"
        L.append(f"- 정찰 범위: 카메라 촬영 면적 약 {ar} · 비행 거리 약 {cv['path_m']:,} m"
                 + (f" · 비행 시간 {cv['dur_s'] // 60}분 {cv['dur_s'] % 60}초" if cv.get("dur_s") else ""))
    L.append("\n## 1. 탐지 요약")
    L.append(f"사람 {d['counts']['person']}명, 차량 {d['counts']['vehicle']}대가 식별되었다."
             + (f" (이 중 {d['counts']['left']}개는 영상 중 카메라 시야 안에서 사라져 '이탈'로 판정)" if d['counts'].get('left') else ""))
    L.append("\n| ID | 종류 | 위도 | 경도 | 오차(m) | 신뢰도 | 탐지 횟수 | 최초~최근 | 감시구역 |" + (" 상태 |" if rp else ""))
    L.append("|---|---|---|---|---|---|---|---|---|" + ("---|" if rp else ""))
    zname = {z["id"]: z["name"] for z in d["zones"]}
    for o in d["objects"]:
        L.append(f"| {o['id']} | {CLS_KO[o['cls']]} | {o['lat']:.6f} | {o['lon']:.6f} | ±{o['err']} | {o['conf']} | "
                 f"{o['hits']} | {o['first_seen']}~{o['last_seen']} | {', '.join(zname.get(z, z) for z in o['zones']) or '-'} |"
                 + ((" 이탈 확인 |" if o.get("left") else (" 기준 시점까지 관측 |" if rp.get("cut") else " 영상 끝까지 관측 |") if o.get("active") else " 시야 밖(미확인) |") if rp else ""))
    L.append("\n## 2. 주요 사건 경과")
    ev = d.get("events") or []
    if ev:
        KEY = {"flight", "alert", "left", "peak", "link", "batt"}
        objs_ev = [e for e in ev if e["type"] == "obj"]
        shown = [e for e in ev if e["type"] in KEY or (e["type"] == "obj" and len(objs_ev) <= 15)]
        if len(objs_ev) > 15:
            L.append(f"- 객체 식별 {len(objs_ev)}건 (최초 {objs_ev[0]['time']}, 마지막 {objs_ev[-1]['time']}) — 개별 목록은 1절 표 참조")
        if len(shown) > 60:
            L.append(f"- (사건이 많아 앞 30건·뒤 30건만 표시, 전체 {len(shown)}건)")
            shown = shown[:30] + shown[-30:]
        L.append("\n| 시각 | 구분 | 내용 |")
        L.append("|---|---|---|")
        KO_EV = {"flight": "기체", "obj": "식별", "alert": "⚠ 구역 침입", "left": "이탈", "peak": "최대 탐지", "link": "연결", "batt": "배터리"}
        for e in shown:
            L.append(f"| {e['time']} | {KO_EV[e['type']]} | {e['text'].replace('|', '/')} |")
    else:
        L.append("- 기록된 사건 없음")
    L.append("\n## 3. 감시구역 경보")
    if d["alerts"]:
        for a in d["alerts"]:
            L.append(f"- {a['time']} [{a['zone']}] {CLS_KO[a['cls']]} {a['obj_id']} 진입 ({a['lat']:.6f}, {a['lon']:.6f})")
    else:
        L.append("- 경보 없음" + ("" if d["zones"] else " (설정된 감시구역 없음)"))
    L.append("\n## 4. 위치추정 신뢰도")
    a = d["accuracy"]
    if a["estimate"]:
        L.append(f"- 기준점 {a['total']}개 중 {a['matched']}개 매칭 · 평균 오차 {a['estimate']['mean']} m · 최대 {a['estimate']['max']} m · "
                 f"CEP50 {a['estimate']['cep50']} m")
        if a["bias"]:
            L.append(f"- 계통 편향: 북 {a['bias']['dn']} m / 동 {a['bias']['de']} m")
    else:
        L.append("- 표의 오차(±m)는 각 객체 위치의 추정 오차 반경(모델 계산값)임 — 실측 기준점 비교값 아님")
    L.append("\n## 5. 시스템 성능")
    p = d["performance"] or {}
    if rp:
        L.append("- 녹화 영상을 지상 노트북에서 사후 분석한 결과라 기체 탑재 성능(FPS·온도)은 해당 없음")
    elif p.get("fps"):
        L.append(f"- 추론 FPS 평균 {p['fps']['avg']} (최소 {p['fps']['min']}) · 추론 {p['infer_ms']['avg'] if p.get('infer_ms') else '-'} ms"
                 f" · 영상→화면 지연 평균 {p['e2e_ms']['avg'] if p.get('e2e_ms') else '-'} ms"
                 f" · Pi 최고 온도 {p['temp_c']['max'] if p.get('temp_c') else '-'} °C")
    else:
        L.append("- 성능 기록 없음")
    L.append("\n## 6. 후속 조치 (권고)")
    L.append("- 신뢰도 낮거나 탐지 횟수가 적은 객체는 재정찰로 확인할 것")
    L.append("- 본 보고서의 좌표는 추정값이며, 오차 반경을 함께 고려할 것")
    if rp:
        L.append("- 감시구역 경보는 현재 대시보드에 설정된 구역 기준으로 영상을 다시 판정한 결과임 (촬영 당시 실시간 경보 아님)")
    return "\n".join(L)


REPORT_PROMPT = """너는 소형 정찰 드론 운용팀의 정보 분석 보조관이다.
아래 JSON은 이번 임무에서 드론(온보드 YOLO 탐지 + 위치추정)이 수집한 결과다.
첨부 이미지는 일부 객체의 탐지 순간 크롭(저해상도)이며, 각 이미지 앞에 객체 ID가 적혀 있다.

한국어 마크다운으로 '정찰 결과 보고서'를 작성하라. 형식:

# 정찰 결과 보고서
## 1. 임무 개요 (시각, 운용 형태, 드론 상태)
## 2. 탐지 요약 (사람/차량 수, 핵심 한 줄)
## 3. 식별 객체 목록 (표: ID, 종류, 좌표, 오차반경, 신뢰도, 탐지 횟수, 관측 시간)
## 4. 주요 객체 관찰 (이미지가 있는 객체만: 외형·색상·주변 상황을 간단히. 해상도가 낮아 불확실하면 '판독 불가/추정'으로 표시)
## 5. 감시구역 경보 (시간순)
## 6. 위치추정 신뢰도 (기준점 검증 통계가 있으면 해석, 없으면 오차 반경이 모델 추정치임을 명시)
## 7. 평가 및 후속 조치 권고 (재정찰 필요 객체, 운용 개선점)

규칙:
- 데이터에 없는 사실을 만들지 말 것. 추정은 '추정'이라고 표시.
- 좌표는 소수점 6자리.
- 운용 형태가 시뮬레이션/벤치 모드이면 개요에 반드시 명시.
- attitude_source가 있으면 임무 개요에 자세 데이터 출처를 한 줄로 언급할 것.
- replay 항목이 있으면 녹화 영상 사후 분석임을 개요 첫 줄에 밝히고, 영상 이름·촬영 시각·길이·탐지 모델을 적을 것.
  replay.cut이 있으면 영상 전체가 아니라 '분석 구간(처음~기준 시점)'의 결과임을 개요 첫 줄에 반드시 밝힐 것.
  이 경우 배터리·기체 성능은 '기록 없음'으로 두고, 감시구역 경보는 현재 설정 구역으로 재판정한 결과라고 적을 것.
- events(사건 일지)가 있으면 '주요 사건 경과' 절을 탐지 요약 다음에 넣어 시간순 표(시각·구분·내용)로 정리할 것.
  객체 식별이 많으면 식별은 묶어서 요약하고 구역 침입·이탈·최대 탐지·기체 사건은 빠짐없이 적을 것.
- coverage가 있으면 임무 개요에 정찰 면적(ha 또는 m²)과 비행 거리를 적을 것.
- left가 true인 객체는 카메라 시야 안에서 사라진 '이탈 확인' 객체, active가 true면 영상 끝까지 관측된 객체,
  둘 다 false면 카메라가 다른 곳을 비춰 확인하지 못한 '시야 밖(미확인)' 객체로 구분해 적을 것.
- 간결하게, 보고서 문체(~함, ~임)로.

임무 데이터:
"""


def ai_report(d, objs, store, model):
    import anthropic

    content = [{"type": "text", "text": REPORT_PROMPT + json.dumps(d, ensure_ascii=False, indent=1)}]
    with_snap = sorted([o for o in objs if o.get("snap")], key=lambda o: -(o.get("conf") or 0))[:6]
    for o in with_snap:
        path = os.path.join(store.snap_dir, f'{o["id"]}.jpg')
        sp = str(o["snap"]).split("?")[0]
        if sp.startswith("/lib/"):                     # 리플레이 항목 스냅샷: /lib/<항목>/snaps/<파일>
            parts = sp.split("/")
            if len(parts) == 5 and parts[3] == "snaps" and ".." not in parts:
                path = os.path.join(replay_lib.LIB, parts[2], "snaps", parts[4])
        elif not sp.startswith("/api/"):                # 기본 데모: web/ 아래 상대 경로
            cand = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", sp.lstrip("/")))
            if cand.startswith(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")):
                path = cand
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
        except OSError:
            continue
        content.append({"type": "text", "text": f"[객체 {o['id']} ({CLS_KO[o['cls']]}) 크롭]"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    client = anthropic.Anthropic()
    resp = client.messages.create(model=model, max_tokens=16000, messages=[{"role": "user", "content": content}])
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
    kinds = ",".join(getattr(b, "type", "?") for b in resp.content)
    u = getattr(resp, "usage", None)
    print(f"[report] AI 응답 model={model} stop={getattr(resp, 'stop_reason', '?')} blocks=[{kinds}] "
          f"in={getattr(u, 'input_tokens', '?')} out={getattr(u, 'output_tokens', '?')} 글자수={len(text)}", flush=True)
    if len(text) < 200 or "#" not in text:
        # 빈 응답(생각만 하고 글을 못 씀 등)이면 규칙 기반으로 대체되게 오류로 넘김
        raise RuntimeError(f"AI 응답에 보고서 본문이 없음 (stop_reason={getattr(resp, 'stop_reason', '?')}, 블록=[{kinds}])")
    return text


def _clean_series(sr):
    """대시보드가 보낸 객체 수 구간 데이터 검사 (길이·형식이 맞지 않으면 버림)"""
    try:
        n = len(sr["t"])
        if not 1 <= n <= 2000:
            return None
        out = dict(bin=float(sr.get("bin") or 1), t=[float(x) for x in sr["t"]], labels=[str(x)[:8] for x in sr["labels"]])
        for k in ("p", "v", "cp", "cv"):
            out[k] = [int(x) for x in sr[k]]
            if len(out[k]) != n:
                return None
        return out if len(out["labels"]) == n else None
    except (KeyError, TypeError, ValueError):
        return None


EV_TYPES = {"flight", "obj", "alert", "left", "peak", "link", "batt"}


def _clean_events(ev):
    out = []
    for e in (ev or [])[-600:]:
        if isinstance(e, dict) and e.get("type") in EV_TYPES:
            out.append(dict(time=str(e.get("time", ""))[:8], type=e["type"], text=str(e.get("text", ""))[:80]))
    return out


def _clean_coverage(c):
    """정찰 범위: 요약(면적·거리·시간)과 그림용 원자료를 나눠 돌려줌"""
    try:
        summ = dict(area_m2=round(float(c["area_m2"])), path_m=round(float(c.get("path_m") or 0)),
                    dur_s=round(float(c.get("dur_s") or 0)))
        full = dict(summ, cell=float(c.get("cell") or 5),
                    cells=[(int(a), int(b)) for a, b in (c.get("cells") or [])[:20000]],
                    path=[(float(a), float(b)) for a, b in (c.get("path") or [])[:2000]],
                    objects=[(str(k)[:1], float(a), float(b)) for k, a, b in (c.get("objects") or [])[:2000]])
        return summ, full
    except (KeyError, TypeError, ValueError):
        return None, None


def insert_charts(text, section):
    """'탐지 요약' 절 끝(다음 ## 제목 앞)에 그래프 절을 끼워 넣음. 못 찾으면 맨 끝에 부록으로."""
    if not section:
        return text
    block = "### 탐지 결과 그래프\n\n" + section + "\n"
    lines = text.split("\n")
    hi = next((i for i, l in enumerate(lines) if l.startswith("## ") and "탐지 요약" in l), None)
    if hi is not None:
        nxt = next((i for i in range(hi + 1, len(lines)) if lines[i].startswith("## ")), None)
        if nxt is not None:
            return "\n".join(lines[:nxt] + [block] + lines[nxt:])
    return text.rstrip() + "\n\n## 부록. 탐지 결과 그래프\n\n" + section + "\n"


# ============================================================ 웹 서버
def camera_footprint(t, cam):
    """현재 자세·고도로 본 카메라 촬영 범위(영상 네 모서리의 지면 좌표). 정찰 범위 표시용."""
    if cam is None or t.get("lat") is None or t.get("alt") is None or t["alt"] < 2.0:
        return None
    pts = []
    for u, v in ((0, 0), (1, 0), (1, 1), (0, 1)):
        g = pixel_to_geo(u, v, t["lat"], t["lon"], t["alt"], t.get("roll") or 0.0, t.get("pitch") or 0.0,
                         t.get("yaw") or 0.0, cam, max_range_m=300.0)
        if g is None:
            return None
        pts.append([round(g[0], 7), round(g[1], 7)])
    return pts


def build_payload(args, tel, vis, trk, perf_last, flog=None, cam=None):
    now = time.monotonic()
    t = tel.snapshot()
    with vis.lock:
        det = vis.last
        stale = now - vis.last_rx > 2.0
    boxes = [] if (det is None or stale) else det["boxes"]

    def fmt(v, nd=1):
        return None if v is None else round(v, nd)

    return {
        "type": "state",
        "time": now_str(),
        "altitude": fmt(t["alt"]), "speed": fmt(t["speed"]),
        "battery": t["battery"], "voltage": fmt(t["voltage"], 2),
        "gps": f'{GPS_FIX.get(t["fix"], "?")} ({t["sats"]})',
        "lat": fmt(t["lat"], 7), "lon": fmt(t["lon"], 7),
        "heading": fmt(t["yaw"], 1), "roll": fmt(t["roll"], 1), "pitch": fmt(t["pitch"], 1),
        "mode": t["mode"], "armed": t["armed"],
        "fps": det["fps"] if det and not stale else 0,
        "seq": det["seq"] if det else 0,
        "detections": {
            "person": sum(1 for b in boxes if b["cls"] == "person"),
            "vehicle": sum(1 for b in boxes if b["cls"] == "vehicle"),
            "other": 0,
        },
        "boxes": boxes,
        "objects": trk.export(now),
        "alerts": list(trk.alerts)[-50:],
        "perf": perf_last,
        "link": {"fc": now - t["last_hb"] < 3.0, "pi": vis.connected and not stale},
        "att_src": getattr(vis, "att_src", None),
        "bench": bool(args.bench_alt),
        "flight": flog.status() if flog else None,
        "footprint": camera_footprint(t, cam) if time.monotonic() - t["last_hb"] < 3.0 else None,
    }


def build_app(args, tel, vis, trk, store, perf, flog):
    clients = set()
    state = {"perf_last": None}

    def json_body_error(msg, code=400):
        return web.json_response({"error": msg}, status=code)

    async def index(request):
        return web.FileResponse(os.path.join(WEB_DIR, "index.html"), headers={"Cache-Control": "no-cache"})   # 업데이트 즉시 반영

    async def config(request):
        video = "" if args.sim_pi else f"http://{args.pi}:{args.pi_video_port}/stream"
        return web.json_response({"video_url": video, "home": args.home,
                                  "sim": args.sim_pi or args.sim_telem, "live": bool(args.pi or args.com),
                                  "ai": bool(os.environ.get("ANTHROPIC_API_KEY"))})

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                    except ValueError:
                        continue
                    if cmd.get("cmd") == "clear_objects":
                        trk.clear()
        finally:
            clients.discard(ws)
        return ws

    async def snap(request):
        name = os.path.basename(request.match_info["name"])
        path = os.path.join(store.snap_dir, name)
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    # ---- ① 기준점 / 정확도
    async def get_accuracy(request):
        return web.json_response(compute_accuracy(store, trk, args.match_radius))

    async def add_truth(request):
        b = await request.json()
        try:
            lat, lon = float(b["lat"]), float(b["lon"])
        except (KeyError, TypeError, ValueError):
            return json_body_error("위도/경도가 올바르지 않습니다")
        if b.get("use_drone"):
            t = tel.snapshot()
            if t["lat"] is None:
                return json_body_error("드론 GPS 위치가 없습니다")
            lat, lon = t["lat"], t["lon"]
        item = dict(id=uuid.uuid4().hex[:6], label=(b.get("label") or f"기준점 {len(store.truth) + 1}")[:30],
                    lat=lat, lon=lon, cls=b.get("cls") or None, obj_id=b.get("obj_id") or None)
        with store.lock:
            store.truth.append(item)
            store.save("truth.json")
        return web.json_response(item)

    async def del_truth(request):
        tid = request.match_info["tid"]
        with store.lock:
            store.truth[:] = [t for t in store.truth if t["id"] != tid]
            store.save("truth.json")
        return web.json_response({"ok": True})

    async def obj_trail(request):
        t = trk.trail_of(request.match_info["oid"])
        if t is None:
            return json_body_error("객체 없음", 404)
        return web.json_response({"id": request.match_info["oid"], "trail": t})

    async def drone_pos(request):
        t = tel.snapshot()
        return web.json_response({"lat": t["lat"], "lon": t["lon"]})

    # ---- ④ 감시구역
    async def get_zones(request):
        return web.json_response(store.zones)

    async def add_zone(request):
        b = await request.json()
        pts = b.get("points") or []
        if len(pts) < 3:
            return json_body_error("꼭짓점이 3개 이상 필요합니다")
        z = dict(id=uuid.uuid4().hex[:6], name=(b.get("name") or f"구역 {len(store.zones) + 1}")[:20],
                 points=[[float(p[0]), float(p[1])] for p in pts])
        with store.lock:
            store.zones.append(z)
            store.save("zones.json")
        trk.recheck_zones()
        return web.json_response(z)

    async def del_zone(request):
        zid = request.match_info["zid"]
        with store.lock:
            store.zones[:] = [z for z in store.zones if z["id"] != zid]
            store.save("zones.json")
        trk.forget_zone(zid)
        return web.json_response({"ok": True})

    # ---- ③ 성능 기록
    async def get_perf(request):
        return web.json_response({"history": list(perf.hist), "summary": perf.summary()})

    # ---- ⑤ 보고서
    async def make_report(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        rp = body.get("replay") if isinstance(body, dict) else None
        if isinstance(rp, dict):
            d, objs = build_replay_report_input(rp, store, args)
        else:
            d, objs = build_report_input(args, tel, trk, store, perf, vis)
        cov_full = None
        if isinstance(body, dict):
            d["events"] = _clean_events(body.get("events"))
            if isinstance(body.get("coverage"), dict):
                d["coverage"], cov_full = _clean_coverage(body["coverage"])
        source, note = "template", None
        text = None
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                text = await asyncio.get_running_loop().run_in_executor(
                    None, ai_report, d, objs, store, args.ai_model)
                source = "claude"
            except ImportError:
                note = "anthropic 패키지가 없어 규칙 기반 보고서로 대체 (pip install anthropic)"
            except Exception as e:
                note = f"AI 호출 실패 → 규칙 기반 보고서로 대체: {e.__class__.__name__}: {e}"
        else:
            note = "ANTHROPIC_API_KEY 미설정 → 규칙 기반 보고서"
        if text is None:
            text = template_report(d)
        try:
            series = _clean_series(body.get("series")) if isinstance(body, dict) and body.get("series") else None
            text = insert_charts(text, report_charts.build_section(dict(d, coverage=cov_full), series))
        except Exception as e:                      # 그래프 실패가 보고서 전체를 막지 않게
            note = (note + " · " if note else "") + f"그래프 생성 실패: {e.__class__.__name__}"
        fn = f'recon_report_{"replay_" if isinstance(rp, dict) else ""}{datetime.now().strftime("%Y%m%d_%H%M%S")}.md'
        try:
            os.makedirs(os.path.join(store.dir, "reports"), exist_ok=True)
            with open(os.path.join(store.dir, "reports", fn), "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        return web.json_response({"report": text, "source": source, "note": note, "filename": fn})

    # ---- ⑥ 비행 기록
    async def flight_list(request):
        return web.json_response({"flights": flog.list(), "status": flog.status()})

    async def flight_get(request):
        rec = flog.get(request.match_info["fid"])
        if rec is None:
            raise web.HTTPNotFound()
        return web.json_response(rec)

    async def flight_start(request):
        return web.json_response({"id": flog.start("수동 (대시보드)"), "status": flog.status()})

    async def flight_stop(request):
        sm = flog.stop("수동 (대시보드)", trk)
        if sm is None:
            return json_body_error("기록 중인 비행이 없습니다")
        return web.json_response({"summary": sm, "status": flog.status()})

    async def bg(app):
        async def push():
            k = 0
            while True:
                await asyncio.sleep(1.0 / args.ui_hz)
                k += 1
                if k % max(1, int(args.ui_hz)) == 0:
                    state["perf_last"] = perf.sample(tel, vis)
                    t = tel.snapshot()
                    flog.tick(t, time.monotonic() - t["last_hb"] < 3.0, trk)
                if not clients:
                    continue
                s = json.dumps(build_payload(args, tel, vis, trk, state["perf_last"], flog, app["cam"]))
                for ws in list(clients):
                    try:
                        await ws.send_str(s)
                    except Exception:
                        clients.discard(ws)

        tasks = [asyncio.create_task(push())]
        if args.pi or args.sim_pi:
            tasks.append(asyncio.create_task(pi_client(args, vis, tel, trk, app["cam"], sim=args.sim_pi, flog=flog)))
        yield
        for t in tasks:
            t.cancel()
        flog.stop("서버 종료", trk)      # 비행 중 지상국을 꺼도 그때까지의 기록은 저장

    @web.middleware
    async def no_cache(request, handler):
        # 화면 파일(html·js·css)은 항상 새로 받게 함 — 업데이트 후 예전 화면이 남는 문제 방지
        resp = await handler(request)
        if request.path == "/" or request.path.endswith((".html", ".js", ".css", ".json")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    app = web.Application(middlewares=[no_cache])
    app["cam"] = Camera(args.hfov, args.vfov, args.cam_rot)
    r = app.router
    r.add_get("/", index)
    r.add_get("/config.json", config)
    r.add_get("/ws", ws_handler)
    r.add_get("/snap/{name}", snap)
    r.add_get("/api/accuracy", get_accuracy)
    r.add_post("/api/truth", add_truth)
    r.add_delete("/api/truth/{tid}", del_truth)
    r.add_get("/api/drone", drone_pos)
    r.add_get("/api/objects/{oid}/trail", obj_trail)
    r.add_get("/api/zones", get_zones)
    r.add_post("/api/zones", add_zone)
    r.add_delete("/api/zones/{zid}", del_zone)
    r.add_get("/api/perf", get_perf)
    r.add_post("/api/report", make_report)
    r.add_get("/api/flights", flight_list)
    r.add_get("/api/flights/{fid}", flight_get)
    r.add_post("/api/flight/start", flight_start)
    r.add_post("/api/flight/stop", flight_stop)
    replay_lib.setup(app, args)          # 자료 보관함 (녹화 영상·비행 로그 재생/분석)
    r.add_static("/", WEB_DIR, show_index=False)
    app.cleanup_ctx.append(bg)
    return app


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--com", help="텔레메트리 포트 (예: COM5, /dev/ttyUSB0)")
    p.add_argument("--baud", type=int, default=57600, help="SiK 텔레메트리 기본 57600")
    p.add_argument("--mp-port", type=int, default=14550, help="Mission Planner 중계 UDP 포트 (0=끄기)")
    p.add_argument("--pi", help="라즈베리파이 IP (hostname -I 로 확인)")
    p.add_argument("--pi-ws-port", type=int, default=8765)
    p.add_argument("--pi-video-port", type=int, default=8081)
    p.add_argument("--telem-from-pi", action="store_true",
                   help="텔레메트리를 파이(UART 직결)에서 받는다 — 433MHz 동글 없이 운용. "
                        "실비행에서는 --com 과 함께 쓰는 것을 권장")
    p.add_argument("--net-delay-ms", type=float, default=80, help="RTT 측정 전 사용할 지연 추정치")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--ui-hz", type=float, default=4)
    p.add_argument("--hfov", type=float, default=66.0)
    p.add_argument("--vfov", type=float, default=41.0)
    p.add_argument("--cam-rot", type=int, default=0, choices=[0, 90, 180, 270])
    p.add_argument("--bench-alt", type=float, default=0.0,
                   help="드론이 지상에 있을 때(고도<1m) 대신 쓸 가상 고도[m]. 0=끄기")
    p.add_argument("--home", default="37.8681,127.7444", help="지도 초기 중심 / 시뮬레이션 기준점")
    p.add_argument("--home-fallback", action="store_true", help="GPS 없을 때 home 좌표로 위치추정 (실내 리허설)")
    p.add_argument("--min-hits", type=int, default=2, help="목록에 올리기 위한 최소 탐지 횟수")
    p.add_argument("--match-radius", type=float, default=10.0, help="기준점 자동 매칭 반경[m]")
    p.add_argument("--data-dir", default=None, help="기준점·감시구역·스냅샷·보고서 저장 폴더")
    p.add_argument("--ai-model", default=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
    p.add_argument("--no-auto-flight", action="store_true",
                   help="ARM/DISARM으로 비행 기록을 자동 시작/종료하지 않음 (대시보드 버튼으로만)")
    p.add_argument("--sim-telem", action="store_true", help="가짜 텔레메트리")
    p.add_argument("--sim-pi", action="store_true", help="가짜 탐지 (파이 없이)")
    args = p.parse_args()
    args.home = [float(x) for x in args.home.split(",")]
    if not (args.com or args.sim_telem or args.telem_from_pi):
        p.error("--com, --sim-telem, --telem-from-pi 중 하나가 필요합니다")
    if args.telem_from_pi and not (args.pi or args.sim_pi):
        p.error("--telem-from-pi 를 쓰려면 --pi 가 필요합니다")
    if not (args.pi or args.sim_pi):
        print("[경고] --pi 가 없어 탐지 없이 텔레메트리만 표시합니다")

    sim = args.sim_telem or args.sim_pi
    data_dir = args.data_dir or os.path.join(BASE_DIR, "data_sim" if sim else "data")
    store = Store(data_dir)
    if args.sim_pi and args.sim_telem and not store.truth:
        # 시뮬레이션: 가짜 표적의 '실제 위치'를 기준점으로 미리 등록 → 정확도 패널 동작 확인용
        for cls, lab, n, e in SIM_TARGETS:
            la, lo = ne_to_ll(args.home, n, e)
            store.truth.append(dict(id=uuid.uuid4().hex[:6], label=f"SIM {lab}", lat=la, lon=lo, cls=cls, obj_id=None))
        store.save("truth.json")

    tel, vis, stop = Telemetry(), Vision(), threading.Event()
    trk = ObjectTracker(store, min_hits=args.min_hits)
    trk.clear()   # 시작할 때 지난 스냅샷 정리 (기준점·구역은 유지)
    perf = PerfLog()
    flog = FlightLog(data_dir, auto=not args.no_auto_flight)

    if args.sim_telem:
        threading.Thread(target=sim_telem_thread, args=(args, tel, stop), daemon=True).start()
    elif args.com:
        threading.Thread(target=mavlink_thread, args=(args, tel, stop), daemon=True).start()
    else:
        print("[fc] --com 없음 — 텔레메트리를 파이(UART 직결)에서 받습니다")

    print(f"[data] {data_dir}")
    print(f"[ai]   {'ANTHROPIC_API_KEY 확인됨 — Claude 보고서' if os.environ.get('ANTHROPIC_API_KEY') else '키 없음 — 규칙 기반 보고서'}")
    print(f"\n  대시보드 →  http://localhost:{args.port}\n")
    web.run_app(build_app(args, tel, vis, trk, store, perf, flog), host="0.0.0.0", port=args.port, print=None)
    stop.set()


if __name__ == "__main__":
    main()
