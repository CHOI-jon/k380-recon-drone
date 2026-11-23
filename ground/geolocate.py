"""
geolocate.py — 방법 B(자세 보정 광선-평면 교차) 간이 구현. 순수 함수, 외부 I/O 없음.

좌표계
  이미지: u 오른쪽(+), v 아래(+), 0~1 정규화
  기체(FRD): x 전방, y 우측, z 하방
  지구(NED): N, E, D

카메라는 수직하방(nadir) 장착, 기본값은 "영상 위쪽 = 기체 전방".
cam_rot(0/90/180/270)으로 실제 장착 방향에 맞춘다 → 지상 확인 절차는 README 참고.
"""
import math
from dataclasses import dataclass

EARTH_R = 6378137.0


@dataclass
class Camera:
    hfov_deg: float = 66.0   # Camera Module 3 표준 (카탈로그값 — 캘리브레이션 후 교체)
    vfov_deg: float = 41.0
    cam_rot: int = 0         # 영상 위쪽이 기체 전방에서 시계방향으로 몇 도 돌아가 있는지


def _rot_zyx(roll, pitch, yaw):
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def pixel_to_geo(u, v, lat, lon, alt_rel, roll_deg, pitch_deg, yaw_deg, cam: Camera,
                 gps_err_m=2.5, max_range_m=500.0):
    """반환: (lat, lon, err_m) 또는 None(광선이 지면과 만나지 않음 / 고도 부족)."""
    if alt_rel is None or alt_rel <= 0.5:
        return None
    # 이미지 평면상 정규화 광선 (핀홀, 왜곡 무시)
    xn = (u - 0.5) * 2 * math.tan(math.radians(cam.hfov_deg) / 2)   # 오른쪽 +
    yn = (v - 0.5) * 2 * math.tan(math.radians(cam.vfov_deg) / 2)   # 아래 +
    # 영상 위(-yn) = 기체 전방, 영상 오른쪽(+xn) = 기체 우측 (cam_rot=0 기준)
    fwd, right = -yn, xn
    a = math.radians(cam.cam_rot)
    bx = fwd * math.cos(a) - right * math.sin(a)
    by = fwd * math.sin(a) + right * math.cos(a)
    d_body = (bx, by, 1.0)

    R = _rot_zyx(math.radians(roll_deg), math.radians(pitch_deg), math.radians(yaw_deg))
    dn = [sum(R[i][j] * d_body[j] for j in range(3)) for i in range(3)]
    if dn[2] <= 0.05:
        return None
    t = alt_rel / dn[2]
    north, east = t * dn[0], t * dn[1]
    ground_range = math.hypot(north, east)
    if ground_range > max_range_m:
        return None

    lat_o = lat + math.degrees(north / EARTH_R)
    lon_o = lon + math.degrees(east / (EARTH_R * math.cos(math.radians(lat))))

    # 간이 오차 예산: GPS + 자세 오차(≈2°, 동기화 포함) × 경사거리 + 픽셀/박스 오차
    slant = math.sqrt(alt_rel ** 2 + ground_range ** 2)
    att_err = slant * math.radians(2.0) * (slant / alt_rel)
    box_err = slant * math.radians(cam.hfov_deg) / 100.0
    err = math.sqrt(gps_err_m ** 2 + att_err ** 2 + box_err ** 2)
    return lat_o, lon_o, err


def distance_m(lat1, lon1, lat2, lon2):
    dn = math.radians(lat2 - lat1) * EARTH_R
    de = math.radians(lon2 - lon1) * EARTH_R * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dn, de)
