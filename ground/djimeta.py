"""
djimeta.py — DJI 영상(MP4/MOV) 안의 비행 데이터 트랙(djmd)을 읽는다. 표준 라이브러리만 사용.

DJI Mavic 4 Pro 등은 영상과 함께 프레임별 비행 데이터를 'djmd' 트랙(protobuf)으로 저장한다.
여기서 위경도·상대고도·기체 자세·짐벌 자세를 꺼낸다. (필드 번호는 10/13 분석으로 확인)
  3.3.4.1.2 / 3.3.4.1.3  위도 / 경도 (double)
  3.3.5.1                상대고도 (mm)
  3.3.3.1 / 2 / 3        기체 피치 / 롤 / 요 (0.1°)
  3.4.3.1                짐벌 피치 (0.1°)
"""
import struct


# ---------------------------------------------------------------- MP4 상자 읽기
def _boxes(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        hlen = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen:
            return
        yield typ, pos + hlen, pos + size
        pos += size


def _find(f, start, end, typ):
    for t, a, b in _boxes(f, start, end):
        if t == typ:
            return a, b
    return None


def _read(f, a, b):
    f.seek(a)
    return f.read(b - a)


def _track_samples(f, trak):
    """trak 상자 → (형식 4글자, [(시각 s, 위치, 크기)])"""
    mdia = _find(f, *trak, b"mdia")
    if not mdia:
        return None, []
    mdhd = _read(f, *_find(f, *mdia, b"mdhd"))
    ver = mdhd[0]
    timescale = struct.unpack(">I", mdhd[12:16] if ver == 0 else mdhd[20:24])[0]
    minf = _find(f, *mdia, b"minf")
    stbl = _find(f, *minf, b"stbl") if minf else None
    if not stbl:
        return None, []
    stsd = _read(f, *_find(f, *stbl, b"stsd"))
    fmt = stsd[12:16] if len(stsd) >= 16 else b""

    stsz = _read(f, *_find(f, *stbl, b"stsz"))
    fixed, n = struct.unpack(">II", stsz[4:12])
    sizes = [fixed] * n if fixed else list(struct.unpack(f">{n}I", stsz[12:12 + 4 * n]))

    co = _find(f, *stbl, b"stco")
    if co:
        d = _read(f, *co)
        m = struct.unpack(">I", d[4:8])[0]
        offs = list(struct.unpack(f">{m}I", d[8:8 + 4 * m]))
    else:
        d = _read(f, *_find(f, *stbl, b"co64"))
        m = struct.unpack(">I", d[4:8])[0]
        offs = list(struct.unpack(f">{m}Q", d[8:8 + 8 * m]))

    d = _read(f, *_find(f, *stbl, b"stsc"))
    m = struct.unpack(">I", d[4:8])[0]
    stsc = [struct.unpack(">III", d[8 + 12 * i:20 + 12 * i]) for i in range(m)]

    d = _read(f, *_find(f, *stbl, b"stts"))
    m = struct.unpack(">I", d[4:8])[0]
    deltas = []
    for i in range(m):
        cnt, dl = struct.unpack(">II", d[8 + 8 * i:16 + 8 * i])
        deltas.extend([dl] * cnt)

    out, k, t = [], 0, 0
    for ci, off in enumerate(offs):
        per = stsc[0][1]
        for j in range(len(stsc)):
            if stsc[j][0] <= ci + 1:
                per = stsc[j][1]
            else:
                break
        pos = off
        for _ in range(per):
            if k >= n:
                break
            out.append((t / timescale, pos, sizes[k]))
            pos += sizes[k]
            t += deltas[k] if k < len(deltas) else 0
            k += 1
    return fmt, out


# ---------------------------------------------------------------- protobuf (스키마 없이 필드 번호 경로로)
def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def _parse(b, depth=0, path="", out=None):
    out = {} if out is None else out
    i = 0
    try:
        while i < len(b):
            key, i = _varint(b, i)
            fno, wt = key >> 3, key & 7
            if fno == 0:
                return None
            p = path + str(fno)
            if wt == 0:
                v, i = _varint(b, i)
                out.setdefault(p, v)
            elif wt == 1:
                out.setdefault(p, struct.unpack("<d", b[i:i + 8])[0])
                i += 8
            elif wt == 5:
                out.setdefault(p, struct.unpack("<f", b[i:i + 4])[0])
                i += 4
            elif wt == 2:
                ln, i = _varint(b, i)
                sub = b[i:i + ln]
                i += ln
                if ln and depth < 6:
                    tmp = {}
                    if _parse(sub, depth + 1, p + ".", tmp) is not None:
                        for k, v in tmp.items():
                            out.setdefault(k, v)
            else:
                return None
        return out if i == len(b) else None
    except (IndexError, struct.error):
        return None


def _s64(v):
    return v - (1 << 64) if v >= (1 << 63) else v


def read_dji_track(path):
    """반환: [{t, lat, lon, alt, dpitch, droll, dyaw, gpitch}] (없으면 빈 목록)"""
    with open(path, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        moov = _find(f, 0, end, b"moov")
        if not moov:
            return []
        for t, a, b in _boxes(f, *moov):
            if t != b"trak":
                continue
            fmt, samples = _track_samples(f, (a, b))
            if fmt != b"djmd":
                continue
            rows = []
            for ts, pos, size in samples:
                f.seek(pos)
                g = _parse(f.read(size))
                if not g or "3.3.4.1.2" not in g or "3.3.5.1" not in g:
                    continue
                lat, lon = g.get("3.3.4.1.2"), g.get("3.3.4.1.3")
                if not lat or not lon:
                    continue
                rows.append(dict(t=round(ts, 4), lat=lat, lon=lon, alt=g["3.3.5.1"] / 1000.0,
                                 dpitch=_s64(g.get("3.3.3.1", 0)) / 10, droll=_s64(g.get("3.3.3.2", 0)) / 10,
                                 dyaw=_s64(g.get("3.3.3.3", 0)) / 10, gpitch=_s64(g.get("3.4.3.1", -900)) / 10))
            return rows
    return []


def has_dji_track(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            moov = _find(f, 0, end, b"moov")
            if not moov:
                return False
            for t, a, b in _boxes(f, *moov):
                if t == b"trak":
                    fmt, _ = _track_samples(f, (a, b))
                    if fmt == b"djmd":
                        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    import sys, time
    t0 = time.time()
    r = read_dji_track(sys.argv[1])
    print(len(r), "rows", f"{time.time() - t0:.1f}s")
    for x in r[:: max(1, len(r) // 8)]:
        print(x)
