"""정찰 보고서용 그래프 — 외부 라이브러리 없이 SVG를 만들어 마크다운 이미지(data URI)로 넣는다.
어두운 배경을 그래프 안에 포함해서 대시보드·VS Code·Typora 등 어디서 열어도 같은 모양으로 보인다."""
import base64
from xml.sax.saxutils import escape

BG, GRID, AXIS, TXT, SUB = "#0f172a", "#1e293b", "#64748b", "#e2e8f0", "#94a3b8"
CP, CV, CZ = "#f43f5e", "#38bdf8", "#f97316"
FONT = "Malgun Gothic, Apple SD Gothic Neo, Noto Sans KR, sans-serif"


def _svg(w, h, body, title, subtitle=""):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="{FONT}">'
            f'<rect width="{w}" height="{h}" rx="12" fill="{BG}"/>'
            f'<text x="20" y="28" fill="{TXT}" font-size="16" font-weight="700">{escape(title)}</text>'
            + (f'<text x="20" y="48" fill="{SUB}" font-size="12">{escape(subtitle)}</text>' if subtitle else "")
            + body + "</svg>")


def _legend(x, y, items):
    out = ""
    for label, c in items:
        out += f'<circle cx="{x}" cy="{y - 4}" r="5" fill="{c}"/><text x="{x + 10}" y="{y}" fill="{SUB}" font-size="12">{escape(label)}</text>'
        x += 22 + 13 * len(label) + 14
    return out


def _nice(top):
    step = 1 if top <= 5 else 2 if top <= 12 else 5 if top <= 30 else 10 if top <= 60 else 20
    return step, max(step, -(-top // step) * step)


def _hms(s):
    try:
        h, m, x = (int(v) for v in str(s)[-8:].split(":"))
        return h * 3600 + m * 60 + x
    except ValueError:
        return None


def line_chart(series, which, title, subtitle, alerts=None, w=860, h=280):
    t, labels = series["t"], series["labels"]
    A, B = (series["p"], series["v"]) if which == "now" else (series["cp"], series["cv"])
    n = len(t)
    if not n:
        return None
    P = dict(l=48, r=20, t=70, b=34)
    tmax = max(t[-1] + series.get("bin", 1), 1)
    step, top = _nice(max(2, max(A), max(B)))
    X = lambda v: P["l"] + v / tmax * (w - P["l"] - P["r"])
    Y = lambda v: h - P["b"] - v / top * (h - P["t"] - P["b"])
    b = ""
    for y in range(0, top + 1, step):
        b += f'<line x1="{P["l"]}" x2="{w - P["r"]}" y1="{Y(y):.1f}" y2="{Y(y):.1f}" stroke="{GRID}"/>'
        b += f'<text x="{P["l"] - 8}" y="{Y(y) + 4:.1f}" fill="{AXIS}" font-size="11" text-anchor="end">{y}</text>'
    for k in range(7):
        i = round(k / 6 * (n - 1))
        anchor = "start" if k == 0 else "end" if k == 6 else "middle"
        b += f'<text x="{X(t[i]):.1f}" y="{h - 12}" fill="{AXIS}" font-size="11" text-anchor="{anchor}">{escape(labels[i])}</text>'
    stepwise = which == "cum"
    for arr, c in ((B, CV), (A, CP)):
        d = ""
        for i, v in enumerate(arr):
            x, y = X(t[i]), Y(v)
            d += f"M{x:.1f},{y:.1f}" if i == 0 else (f"H{x:.1f}V{y:.1f}" if stepwise else f"L{x:.1f},{y:.1f}")
        if not stepwise:
            b += f'<path d="{d}L{X(t[-1]):.1f},{Y(0):.1f}L{X(t[0]):.1f},{Y(0):.1f}Z" fill="{c}" fill-opacity="0.13"/>'
        b += f'<path d="{d}" fill="none" stroke="{c}" stroke-width="2" stroke-linejoin="round"/>'
        if stepwise and arr:
            b += f'<text x="{w - P["r"]:.1f}" y="{Y(arr[-1]) - 6:.1f}" fill="{c}" font-size="12" font-weight="700" text-anchor="end">{arr[-1]}</text>'
    legend = [("사람", CP), ("차량", CV)]
    if alerts:
        base = _hms(labels[0])
        marks = 0
        for a in alerts:
            if a.get("t") is not None:
                at = float(a["t"])
            else:
                s = _hms(a.get("time", ""))
                at = None if s is None or base is None else (s - base) % 86400
            if at is None or at > tmax:
                continue
            x = X(at)
            b += f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{P["t"]}" y2="{h - P["b"]}" stroke="{CZ}" stroke-width="1" stroke-dasharray="3 3" opacity="0.8"/>'
            b += f'<path d="M{x - 5:.1f},{P["t"] - 2}L{x + 5:.1f},{P["t"] - 2}L{x:.1f},{P["t"] + 6}Z" fill="{CZ}"/>'
            marks += 1
        if marks:
            legend.append(("감시구역 침입", CZ))
    b += _legend(w - 330, 30, legend)
    return _svg(w, h, b, title, subtitle)


def status_chart(objects, replay, w=860, h=None, cut=False):
    """종류별 객체 상태 — 가로 누적 막대"""
    if replay:
        cats = [("이탈 확인", lambda o: o.get("left"), "#64748b"),
                ("기준 시점까지 관측" if cut else "영상 끝까지 관측", lambda o: not o.get("left") and o.get("active"), "#22c55e"),
                ("시야 밖(미확인)", lambda o: not o.get("left") and not o.get("active"), "#a78bfa")]
    else:
        cats = [("추적 중", lambda o: o.get("active"), "#22c55e"), ("시야 밖", lambda o: not o.get("active"), "#a78bfa")]
    rows = [("사람", [o for o in objects if o["cls"] == "person"]), ("차량", [o for o in objects if o["cls"] == "vehicle"])]
    total = max(1, max(len(r[1]) for r in rows))
    h = h or 222
    L, R, y0, bh = 80, 70, 78, 34
    b = ""
    for k, (name, objs) in enumerate(rows):
        y = y0 + k * (bh + 22)
        b += f'<text x="{L - 12}" y="{y + bh / 2 + 5}" fill="{TXT}" font-size="14" text-anchor="end" font-weight="700">{name}</text>'
        x = L
        for label, f, c in cats:
            cnt = sum(1 for o in objs if f(o))
            if not cnt:
                continue
            ww = cnt / total * (w - L - R)
            b += f'<rect x="{x:.1f}" y="{y}" width="{ww:.1f}" height="{bh}" fill="{c}" rx="3"/>'
            if ww > 22:
                b += f'<text x="{x + ww / 2:.1f}" y="{y + bh / 2 + 5}" fill="#0f172a" font-size="13" font-weight="800" text-anchor="middle">{cnt}</text>'
            x += ww
        b += f'<text x="{x + 10:.1f}" y="{y + bh / 2 + 5}" fill="{SUB}" font-size="13">계 {len(objs)}</text>'
    b += _legend(20, h - 14, [(c[0], c[2]) for c in cats])
    return _svg(w, h, b, "식별 객체 구성", ("종류별 객체 수와 기준 시점의 상태" if cut else "종류별 객체 수와 영상 종료 시점의 상태") if replay else "종류별 객체 수와 현재 추적 상태")


def zone_chart(alerts, zones, w=860):
    if not alerts:
        return None
    names = [z["name"] for z in zones] or sorted({a["zone"] for a in alerts})
    for a in alerts:
        if a["zone"] not in names:
            names.append(a["zone"])
    rows = [(n, sum(1 for a in alerts if a["zone"] == n and a["cls"] == "person"),
             sum(1 for a in alerts if a["zone"] == n and a["cls"] == "vehicle")) for n in names]
    h = 80 + len(rows) * 44 + 30
    top = max(1, max(p + v for _, p, v in rows))
    L, R = 130, 70
    b = ""
    for k, (n, p, v) in enumerate(rows):
        y = 70 + k * 44
        b += f'<text x="{L - 12}" y="{y + 19}" fill="{TXT}" font-size="13" text-anchor="end">{escape(n[:12])}</text>'
        x = L
        for cnt, c in ((p, CP), (v, CV)):
            if cnt:
                ww = cnt / top * (w - L - R)
                b += f'<rect x="{x:.1f}" y="{y}" width="{ww:.1f}" height="28" fill="{c}" rx="3"/>'
                if ww > 20:
                    b += f'<text x="{x + ww / 2:.1f}" y="{y + 19}" fill="#0f172a" font-size="12" font-weight="800" text-anchor="middle">{cnt}</text>'
                x += ww
        b += f'<text x="{x + 10:.1f}" y="{y + 19}" fill="{SUB}" font-size="13">{p + v}건</text>'
    b += _legend(20, h - 14, [("사람", CP), ("차량", CV)])
    return _svg(w, h, b, "감시구역별 침입", "구역에 들어온 객체 수 (종류별)")


def coverage_chart(cov, w=860, h=520):
    """정찰 범위 지도: 촬영한 땅(하늘색 칸) + 드론 경로 + 식별 객체 위치 (북쪽 위)"""
    cells, cs = cov.get("cells") or [], float(cov.get("cell") or 5)
    if not cells:
        return None
    xs = [c[0] * cs for c in cells] + [p[0] for p in cov.get("path") or []]
    ys = [c[1] * cs for c in cells] + [p[1] for p in cov.get("path") or []]
    x0, x1, y0, y1 = min(xs), max(xs) + cs, min(ys), max(ys) + cs
    P = dict(l=20, r=20, t=64, b=40)
    sc = min((w - P["l"] - P["r"]) / max(x1 - x0, 1), (h - P["t"] - P["b"]) / max(y1 - y0, 1))
    ox = P["l"] + ((w - P["l"] - P["r"]) - (x1 - x0) * sc) / 2
    oy = P["t"] + ((h - P["t"] - P["b"]) - (y1 - y0) * sc) / 2
    X = lambda x: ox + (x - x0) * sc
    Y = lambda y: oy + (y1 - y) * sc
    sq = max(cs * sc, 0.6)
    d = "".join(f"M{X(i * cs):.1f},{Y((j + 1) * cs):.1f}h{sq:.1f}v{sq:.1f}h{-sq:.1f}z" for i, j in cells)
    b = f'<path d="{d}" fill="#22d3ee" fill-opacity="0.30"/>'
    path = cov.get("path") or []
    if len(path) > 1:
        b += '<polyline points="' + " ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in path) + '" fill="none" stroke="#facc15" stroke-width="1.6" stroke-opacity="0.9"/>'
        b += f'<circle cx="{X(path[0][0]):.1f}" cy="{Y(path[0][1]):.1f}" r="5" fill="#facc15"/>'
        b += f'<text x="{X(path[0][0]) + 8:.1f}" y="{Y(path[0][1]) + 4:.1f}" fill="#fde68a" font-size="11">시작</text>'
    for o in cov.get("objects") or []:
        c = CP if o[0] == "p" else CV
        b += f'<circle cx="{X(o[1]):.1f}" cy="{Y(o[2]):.1f}" r="3.2" fill="{c}" stroke="#0f172a" stroke-width="0.8"/>'
    # 축척 막대 + 북쪽 표시
    span = (x1 - x0)
    bar = next(v for v in (10, 20, 50, 100, 200, 500, 1000, 2000) if v >= span / 6)
    bx, by = 24, h - 18
    b += f'<line x1="{bx}" x2="{bx + bar * sc:.1f}" y1="{by}" y2="{by}" stroke="{TXT}" stroke-width="2"/>'
    b += f'<text x="{bx + bar * sc + 8:.1f}" y="{by + 4}" fill="{SUB}" font-size="11">{bar} m</text>'
    b += f'<path d="M{w - 34},{P["t"] + 20}l8,-18l8,18l-8,-5z" fill="{TXT}"/><text x="{w - 26}" y="{P["t"] + 34}" fill="{SUB}" font-size="11" text-anchor="middle">N</text>'
    b += _legend(w - 380, 30, [("촬영 범위", "#22d3ee"), ("드론 경로", "#facc15"), ("사람", CP), ("차량", CV)])
    area = cov.get("area_m2") or 0
    sub = (f"정찰 면적 {area / 10000:.2f} ha" if area >= 10000 else f"정찰 면적 {area:,.0f} m²") + \
          f" · 비행 거리 {cov.get('path_m', 0):,} m · 북쪽이 위"
    return _svg(w, h, b, "정찰 범위", sub)


def img(svg, alt):
    return f"![{alt}](data:image/svg+xml;base64,{base64.b64encode(svg.encode('utf-8')).decode()})"


def build_section(d, series):
    """보고서에 넣을 '그래프' 절 마크다운. 그릴 것이 없으면 빈 문자열."""
    replay = bool(d.get("replay"))
    out = []
    if series and series.get("t"):
        n = len(series["t"])
        ip = max(range(n), key=lambda i: series["p"][i]); iv = max(range(n), key=lambda i: series["v"][i])
        sub = (f"한 화면에 동시에 잡힌 수 (구간 최대값) · 최대 사람 {series['p'][ip]}명({series['labels'][ip]}), "
               f"차량 {series['v'][iv]}대({series['labels'][iv]})")
        s1 = line_chart(series, "now", "시간대별 탐지 수", sub, alerts=d.get("alerts"))
        s2 = line_chart(series, "cum", "누적 식별 객체 수", "2회 이상 탐지되어 객체로 확정된 수의 누적")
        if s1:
            out.append(img(s1, "시간대별 탐지 수"))
            out.append(f"- 사람은 {series['labels'][ip]}에 최대 {series['p'][ip]}명, 차량은 {series['labels'][iv]}에 최대 {series['v'][iv]}대가 한 화면에 동시에 탐지됨"
                       + (" (주황 표시 = 감시구역 침입 시각)" if d.get("alerts") else ""))
        if s2:
            out.append(img(s2, "누적 식별 객체 수"))
    cov = d.get("coverage")
    if cov:
        c = coverage_chart(cov)
        if c:
            out.insert(0, img(c, "정찰 범위"))
    if d.get("objects"):
        out.append(img(status_chart(d["objects"], replay, cut=bool((d.get("replay") or {}).get("cut"))), "식별 객체 구성"))
    z = zone_chart(d.get("alerts") or [], d.get("zones") or [])
    if z:
        out.append(img(z, "감시구역별 침입"))
    return "\n\n".join(out)
