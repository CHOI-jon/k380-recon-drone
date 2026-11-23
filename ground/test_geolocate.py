from geolocate import Camera, pixel_to_geo, distance_m, EARTH_R
import math

cam = Camera()
LAT, LON = 37.8681, 127.7444

def ne(res):
    la, lo, _ = res
    return (math.radians(la - LAT) * EARTH_R,
            math.radians(lo - LON) * EARTH_R * math.cos(math.radians(LAT)))

# 1) 수평, 영상 중심 → 드론 바로 아래
n, e = ne(pixel_to_geo(0.5, 0.5, LAT, LON, 30, 0, 0, 0, cam))
assert abs(n) < 1e-6 and abs(e) < 1e-6
# 2) 영상 위쪽 → 기체 전방(북쪽, yaw=0)
n, e = ne(pixel_to_geo(0.5, 0.0, LAT, LON, 30, 0, 0, 0, cam))
assert abs(n - 30 * math.tan(math.radians(20.5))) < 0.05 and abs(e) < 1e-6
# 3) 영상 오른쪽 + yaw 90(동쪽) → 남쪽
n, e = ne(pixel_to_geo(1.0, 0.5, LAT, LON, 30, 0, 0, 90, cam))
assert n < -19 and abs(e) < 1e-6
# 4) 기수 들림(pitch +10) → 영상 중심이 전방을 봄
n, e = ne(pixel_to_geo(0.5, 0.5, LAT, LON, 30, 0, 10, 0, cam))
assert abs(n - 30 * math.tan(math.radians(10))) < 0.05
# 5) 우측 롤(+10) → 중심이 우측(동쪽)... 오른쪽으로 기울면 배가 왼쪽을 향함
n, e = ne(pixel_to_geo(0.5, 0.5, LAT, LON, 30, 10, 0, 0, cam))
assert abs(e + 30 * math.tan(math.radians(10))) < 0.05
# 6) 고도 0 → None
assert pixel_to_geo(0.5, 0.5, LAT, LON, 0, 0, 0, 0, cam) is None
print("geolocate tests OK")
