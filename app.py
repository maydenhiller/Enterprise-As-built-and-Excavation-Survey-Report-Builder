# ======================================================================
# CORE: stations, GPS shots, coatings
# ======================================================================
import csv
import io
import math
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# Stationing
# ----------------------------------------------------------------------
_STA_RE = re.compile(r"(\d{1,6})\s*\+\s*(\d{2})(?:\.\d+)?")


def parse_station(text: str) -> Optional[int]:
    """'12297+86' -> 1229786 (whole feet). None if no station found."""
    m = _STA_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)) * 100 + int(m.group(2))


def fmt_station(feet: int) -> str:
    """1229786 -> '12297+86'."""
    neg = feet < 0
    feet = abs(int(feet))
    return ("-" if neg else "") + f"{feet // 100}+{feet % 100:02d}"


def round_half_up(x: float) -> int:
    """Whole-foot rounding used on the existing as-builts: .5 rounds UP
    (86 + 0.50 -> 87, 35 + 5.50 -> 41)."""
    return int(math.floor(x + 0.5 + 1e-9))


def station_at(us_station_ft: int, dist_ft: float) -> int:
    return round_half_up(us_station_ft + dist_ft)


# ----------------------------------------------------------------------
# Coating types -> colour.  Edit this table (or use the sidebar editor).
# ----------------------------------------------------------------------
# colour is the pipe-edge colour of the gradient; the centre is a brighter
# version of the same hue (PROTAL-7200 measured off the existing as-builts:
# edge #00B050, centre ~#00EA6A).
DEFAULT_COATINGS: Dict[str, str] = {
    "PROTAL-7200": "#00B050",     # lime green (centre colour verified on examples)
    "DENSO 7200": "#00B050",      # lime green
    "SPC 4888": "#8B5A2B",        # brown
    "SPC 2831": "#BFBFBF",        # light gray
}

# coating already on the pipe wherever the repair coating is not (no measurements)
EXISTING_COATINGS = ["Coal Tar", "Polyken Tape"]


def brighten(hex_color: str, k: float = 1.33) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    if max(r, g, b) < 60:                      # black: lighten a little so the cylinder still shows shading
        return "#404040"
    if (r + g + b) / 3 > 170:                  # already light: don't blow out to white
        k = 1.1
    return "#%02X%02X%02X" % tuple(min(255, int(round(c * k))) for c in (r, g, b))


# ----------------------------------------------------------------------
# Sleeve types offered in the dropdown (label used on callouts + sleeve)
# ----------------------------------------------------------------------
SLEEVE_TYPES = ["Petro Sleeve", "B Sleeve", "Other..."]


def begin_end_labels(sleeve_type: str) -> Tuple[str, str]:
    """('Begin Petro', 'Sleeve') style two-line labels like the as-builts."""
    t = sleeve_type.strip()
    m = re.match(r"^(.*?)\s*sleeve$", t, re.I)
    if m and m.group(1):
        return m.group(1).strip(), "Sleeve"
    return t, ""


# ----------------------------------------------------------------------
# Raw GPS shots
# ----------------------------------------------------------------------
@dataclass
class Shot:
    name: str
    created: str
    lat: float
    lon: float
    elev: float
    h_acc: Optional[float] = None
    v_acc: Optional[float] = None
    # filled by classify_for_dig
    role: str = "other"          # weld | sleeve | anomaly | other
    side: Optional[str] = None   # us | ds | None
    idx: Optional[int] = None
    kind_hint: Optional[str] = None   # Petro Sleeve / B Sleeve
    strong: bool = True          # dig token matched with its letter

    @property
    def label(self) -> str:
        acc = f"±{self.h_acc:.2f} ft" if self.h_acc is not None else ""
        when = self.created[5:16].replace("T", " ") if self.created else ""
        return f"{self.name.strip()}  |  {self.lat:.8f}, {self.lon:.8f}  |  {acc}  {when}".strip()

    @property
    def day(self) -> Optional[date]:
        try:
            return datetime.fromisoformat(self.created[:19]).date()
        except Exception:
            return None


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def load_shots(data: bytes) -> List[Shot]:
    """Read the raw GNSS export.  Header looks like:
    AGMName,CreatedOn,,1,YPosition,XPosition,ZPosition,Estimatedaccuracy,
    Estverticalaccuracy,X,Y,Z   (X = longitude, Y = latitude, 8 decimals)."""
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    low = [h.lower() for h in head]

    def col(*names, last=False):
        idxs = [i for i, h in enumerate(low) if h in names]
        if not idxs:
            return None
        return idxs[-1] if last else idxs[0]

    c_name = col("agmname", "name", "point", "pointname")
    c_time = col("createdon", "created", "time")
    c_x = col("x", last=True)
    c_y = col("y", last=True)
    c_z = col("z", last=True)
    c_xp, c_yp, c_zp = col("xposition"), col("yposition"), col("zposition")
    c_ha, c_va = col("estimatedaccuracy"), col("estverticalaccuracy")
    shots = []
    for r in rows[1:]:
        if not r or c_name is None or c_name >= len(r) or not r[c_name].strip():
            continue

        def g(i):
            return r[i] if i is not None and i < len(r) else None

        lon = _f(g(c_x)); lat = _f(g(c_y)); z = _f(g(c_zp)) if c_zp is not None else None
        if lon is None:
            lon = _f(g(c_xp))
        if lat is None:
            lat = _f(g(c_yp))
        if z is None:
            z = _f(g(c_z))
        if lon is None or lat is None:
            continue
        shots.append(Shot(name=r[c_name], created=(g(c_time) or ""), lat=lat, lon=lon,
                          elev=z if z is not None else float("nan"),
                          h_acc=_f(g(c_ha)), v_acc=_f(g(c_va))))
    return shots


def dig_tokens(dig: str) -> Tuple[Optional[int], str]:
    m = re.match(r"^\s*0*(\d+)\s*([A-Za-z]*)\s*$", dig or "")
    if not m:
        return None, ""
    return int(m.group(1)), m.group(2).upper()


def _dig_regexes(num: int, letter: str):
    n = rf"0*{num}"
    strong = re.compile(rf"(?<![0-9A-Za-z]){n}\s*{letter}(?![A-Za-z0-9])", re.I) if letter else None
    weak = re.compile(rf"(?<![0-9A-Za-z]){n}(?![A-Za-z0-9])", re.I)
    return strong, weak


def classify_for_dig(shots: List[Shot], dig: str) -> List[Shot]:
    """Return COPIES of the shots whose name refers to this dig, with role,
    side (us/ds), index and sleeve-kind guessed from the name."""
    num, letter = dig_tokens(dig)
    if num is None:
        return []
    strong, weak = _dig_regexes(num, letter)
    out = []
    for s in shots:
        name = s.name
        m = strong.search(name) if strong else None
        is_strong = bool(m)
        if not m:
            m = weak.search(name)
            if m:
                # letterless match ("15 u/s gw pipe" for dig 15A) is only
                # believable if the name carries no other dig token
                # ("49A US sleeve 3" must not count for dig 3A) and the
                # number is not a sleeve index.
                others = [x for x in re.finditer(r"(?<![0-9A-Za-z])\d+\s*[A-Za-z](?![A-Za-z0-9])", name)
                          if x.start() != m.start()]
                if others or re.search(r"sleeve\s*$", name[:m.start()], re.I):
                    m = None
        if not m:
            continue
        rest = (name[:m.start()] + " " + name[m.end():]).lower()
        rest = re.sub(r"\baid\s*\d+\b", " ", rest)
        rest = re.sub(r"\bdig\b", " ", rest)
        txt = re.sub(r"[^a-z0-9/ ]", " ", rest)
        txt = re.sub(r"\s+", " ", txt).strip()
        c = Shot(**{k: getattr(s, k) for k in ("name", "created", "lat", "lon", "elev", "h_acc", "v_acc")})
        c.strong = is_strong
        us = bool(re.search(r"(?<![a-z])u/?s(?![a-z])|usgw|ussleeve|(?<![a-z])us(?=b?sleeve)|upstream", txt))
        ds = bool(re.search(r"(?<![a-z])d/?s(?![a-z])|dsgw|dssleeve|(?<![a-z])ds(?=b?sleeve|petro)|downstream", txt))
        c.side = "us" if us and not ds else "ds" if ds and not us else None
        if "sleeve" in txt or "sleev" in txt:
            c.role = "sleeve"
            mi = re.search(r"sleeve\s*(\d+)", txt)
            c.idx = int(mi.group(1)) if mi else None
            if "petro" in txt:
                c.kind_hint = "Petro Sleeve"
            elif re.search(r"(?<![a-z])b\s?sleeve|bsleeve|type b", txt):
                c.kind_hint = "B Sleeve"
        elif "anomal" in txt:
            c.role = "anomaly"
        elif re.search(r"(?<![a-z])(g/?w|girth)(?![a-z])|usgw|dsgw|\bweld\b", txt) and "cross" not in txt:
            c.role = "weld"
        else:
            c.role = "other"
        out.append(c)
    return out


def centroid(shots: List[Shot]) -> Optional[Tuple[float, float, float]]:
    if not shots:
        return None
    n = len(shots)
    return (sum(s.lat for s in shots) / n, sum(s.lon for s in shots) / n,
            sum(s.elev for s in shots if not math.isnan(s.elev)) / max(1, sum(1 for s in shots if not math.isnan(s.elev))))


def suggest_weld(cands: List[Shot], side: str) -> Optional[Shot]:
    """Best guess for a weld shot: explicit US/DS named shots only, newest wins."""
    pool = [s for s in cands if s.role == "weld" and s.side == side]
    if not pool:
        return None
    pool.sort(key=lambda s: (s.strong, s.created))
    return pool[-1]


def suggest_sleeves(cands: List[Shot], n: int) -> List[List[Shot]]:
    """One list of shots per sleeve (ordered upstream -> downstream)."""
    sl = [s for s in cands if s.role == "sleeve"]
    anom = [s for s in cands if s.role == "anomaly"]
    if n <= 0:
        return []
    if not sl:
        return [anom[-1:]] + [[] for _ in range(n - 1)] if anom else [[] for _ in range(n)]
    if n == 1:
        return [sl]
    groups: Dict[tuple, List[Shot]] = {}
    for s in sl:
        groups.setdefault((s.side or "", s.idx or 0), []).append(s)
    order = {"us": 0, "": 1, "ds": 2}
    keys = sorted(groups, key=lambda k: (order[k[0]], k[1]))
    if len(keys) == n:
        return [groups[k] for k in keys]
    by_side = {"us": [s for s in sl if s.side == "us"], "ds": [s for s in sl if s.side == "ds"]}
    if n == 2 and by_side["us"] and by_side["ds"]:
        return [by_side["us"], by_side["ds"]]
    by_idx: Dict[int, List[Shot]] = {}
    for s in sl:
        if s.idx:
            by_idx.setdefault(s.idx, []).append(s)
    if len(by_idx) == n:
        return [by_idx[k] for k in sorted(by_idx)]
    return [[] for _ in range(n)]
# ======================================================================
# REPAIR MODEL + VERTICAL LAYOUT (all page units are inches, y up)
# ======================================================================
@dataclass
class SleeveIn:
    type: str = "Petro Sleeve"
    heat: str = ""
    begin: float = 0.0            # ft from U/S weld
    end: float = 0.0              # ft from U/S weld
    gps: Optional[Tuple[float, float]] = None   # (lat, lon) centre of repair


@dataclass
class RepairIn:
    coating_name: str = "PROTAL-7200"
    coating_color: str = "#00B050"
    cb: float = 0.0               # begin coating, ft from U/S weld (may be negative)
    ce: float = 0.0               # end coating
    sleeves: List[SleeveIn] = field(default_factory=list)
    center_gps: Optional[Tuple[float, float]] = None   # used when there are no sleeves
    depth_text: str = ""
    name: str = ""
    date_text: str = ""           # mm/dd/yy
    cuts: List[Tuple[float, float]] = field(default_factory=list)   # all tape cuts (begin, end); cb/ce = outer envelope
    existing: str = ""            # "Coal Tar" / "Polyken Tape" / "" (existing coating on pipe)


# page constants measured off the Enterprise as-built template
PIPE_CX = 3.7309
PIPE_R = 0.2300
COAT_R = 0.2500
SLEEVE_R = 0.2684
BAND_THICK = 0.0517
US_Y0 = 0.43
DS_Y0 = 8.815
DIM_X_MAX = 6.05
BOX_RIGHT = 1.45
LINE_H = 0.1333           # one 8 pt text line


def cut_list(rep: RepairIn) -> List[Tuple[float, float]]:
    return sorted(rep.cuts) if rep.cuts else [(rep.cb, rep.ce)]


def validate_repair(rep: RepairIn, joint_len_ft: Optional[float]) -> List[str]:
    w = []
    prev_end = None
    for k, (a, b) in enumerate(cut_list(rep), 1):
        if b <= a:
            w.append(f"Tape cut {k}: end must be greater than begin.")
        if prev_end is not None and a < prev_end - 1e-9:
            w.append(f"Tape cut {k} overlaps the previous cut.")
        prev_end = b
    last_end = None
    for i, s in enumerate(rep.sleeves, 1):
        if s.end <= s.begin:
            w.append(f"Sleeve {i}: end must be greater than begin.")
        if s.begin < rep.cb - 1e-9 or s.end > rep.ce + 1e-9:
            w.append(f"Sleeve {i} runs outside the coating ({rep.cb:.2f} to {rep.ce:.2f} ft).")
        if last_end is not None and s.begin < last_end - 1e-9:
            w.append(f"Sleeve {i} overlaps sleeve {i - 1} - enter sleeves upstream to downstream.")
        last_end = s.end
    if joint_len_ft and max([rep.ce] + [s.end for s in rep.sleeves]) > joint_len_ft + 0.5:
        w.append(f"A measurement is past the D/S weld (joint is about {joint_len_ft:.0f} ft).")
    return w


@dataclass
class Layout:
    y_us: float
    y_ds: float
    y_of: Dict[float, float]           # distance(ft) -> y (in)
    pipe_bottom: float
    pipe_top: float

    def y(self, d: float) -> float:
        return self.y_of[round(d, 4)]


def compute_layout(rep: RepairIn) -> Layout:
    feats = {0.0, round(rep.cb, 4), round(rep.ce, 4)}
    for a, b in cut_list(rep):
        feats.add(round(a, 4)); feats.add(round(b, 4))
    for s in rep.sleeves:
        feats.add(round(s.begin, 4)); feats.add(round(s.end, 4))
    F = sorted(feats)
    sleeve_pairs = {(round(s.begin, 4), round(s.end, 4)) for s in rep.sleeves}
    # (distance a, distance b, min inches, cap inches)
    gaps = []
    for a, b in zip(F, F[1:]):
        g = b - a
        if g < 0.02:
            lo = 0.0
        elif (a, b) in sleeve_pairs:
            lo = 1.30
        else:
            lo = 0.40
        gaps.append([a, b, g, lo, 2.4])

    def build(s, lo_scale):
        ws = []
        for a, b, g, lo, cap in gaps:
            w = max(lo * lo_scale, min(cap, s * g)) if g >= 0.02 else 0.0
            ws.append(w)
        return ws

    below = sum(1 for a, b, *_ in gaps if b <= 0)   # gaps wholly below the weld
    # y of the weld: leave room below it when the coating starts upstream of it
    def y_weld(ws):
        extra = sum(w for (a, b, *_), w in zip(gaps, ws) if b <= 0)
        return US_Y0 + (min(extra, 1.6) + 0.25 if extra > 0 else 0.0), extra

    s = 1.25
    lo_scale = 1.0
    for _ in range(80):
        ws = build(s, lo_scale)
        yw, extra = y_weld(ws)
        above = sum(w for (a, b, *_), w in zip(gaps, ws) if a >= 0)
        if yw + above <= DS_Y0 - 0.65 and extra <= 1.6:
            break
        s *= 0.93
        if s < 0.12:
            lo_scale *= 0.93
    ws = build(s, lo_scale)
    yw, extra = y_weld(ws)
    if extra > 1.6 and extra > 0:      # squeeze the part below the weld
        k = 1.6 / extra
        ws = [w * k if b <= 0 else w for (a, b, *_), w in zip(gaps, ws)]
        yw, extra = y_weld(ws)
    y_of = {0.0: yw}
    # upward
    cur = yw
    for (a, b, *_), w in zip(gaps, ws):
        if a >= 0:
            cur += w
            y_of[b] = cur
    # downward
    cur = yw
    for (a, b, *_), w in reversed(list(zip(gaps, ws))):
        if b <= 0:
            cur -= w
            y_of[a] = cur
    lo_y = min(y_of.values())
    pipe_bottom = (lo_y - 0.25) if lo_y < yw - 1e-6 else (yw - 0.1727)
    return Layout(y_us=yw, y_ds=DS_Y0, y_of=y_of, pipe_bottom=pipe_bottom, pipe_top=DS_Y0 + 0.1685)


# ----------------------------------------------------------------------
# bands
# ----------------------------------------------------------------------
def band_positions(rep: RepairIn, lay: Layout) -> List[Tuple[float, bool]]:
    """[(y, is_outer_far)] metal bands on each side of every sleeve."""
    if not rep.sleeves:
        return []
    sl = sorted(rep.sleeves, key=lambda s: s.begin)
    ys = [(lay.y(s.begin), lay.y(s.end)) for s in sl]
    y_cb, y_ce = lay.y(rep.cb), lay.y(rep.ce)
    out: List[Tuple[float, bool]] = []
    m = 0.10    # keep clear of coating end / sleeve edge

    def one_sided(y_sleeve, y_far, up):
        G = abs(y_far - y_sleeve)
        sign = 1 if up else -1
        if G >= 1.1:
            offs = [min(.25, .15 * G), min(.5, .30 * G), min(1.35, .75 * G)]
        elif G >= 0.45:
            offs = [.30 * G, .65 * G]
        elif G >= 0.22:
            offs = [.5 * G]
        else:
            offs = []
        return [(y_sleeve + sign * o, k == len(offs) - 1 and len(offs) == 3) for k, o in enumerate(offs)]

    # below the first sleeve
    out += one_sided(ys[0][0], y_cb, up=False)
    # above the last sleeve
    out += one_sided(ys[-1][1], y_ce, up=True)
    # between sleeves
    for (b0, e0), (b1, e1) in zip(ys, ys[1:]):
        G = b1 - e0
        if G >= 2.0:
            out += [(e0 + .25, False), (e0 + .5, False), (b1 - .25, False), (b1 - .5, False)]
        elif G >= 1.0:
            out += [(e0 + .25, False), (b1 - .25, False)]
        elif G >= 0.3:
            out += [((e0 + b1) / 2, False)]
    return out


# ----------------------------------------------------------------------
# left-hand callout packing
# ----------------------------------------------------------------------
def pack_boxes(items: List[dict], lo: float, hi: float, gap: float = 0.05) -> None:
    """items: dicts with 'y_feat','h' ; sets item['y'] (centre) so boxes never
    overlap, staying as close to y_feat as possible."""
    items.sort(key=lambda it: it["y_feat"])
    clusters = []   # each: [members, centre]
    for it in items:
        clusters.append({"m": [it], "c": it["y_feat"]})
        while len(clusters) > 1:
            a, b = clusters[-2], clusters[-1]
            ha = sum(i["h"] for i in a["m"]) + gap * (len(a["m"]) - 1)
            hb = sum(i["h"] for i in b["m"]) + gap * (len(b["m"]) - 1)
            # cluster extents when centred on their wanted position
            top_a = a["c"] + ha / 2
            bot_b = b["c"] - hb / 2
            if bot_b >= top_a + gap - 1e-9:
                break
            # merge: centre on the mean of members' wanted y
            members = a["m"] + b["m"]
            c = sum(i["y_feat"] for i in members) / len(members)
            clusters[-2:] = [{"m": members, "c": c}]
    # lay out each cluster, then clamp to [lo, hi]
    placed = []
    for cl in clusters:
        h = sum(i["h"] for i in cl["m"]) + gap * (len(cl["m"]) - 1)
        y = cl["c"] - h / 2            # bottom of the cluster; members ascend
        for i in cl["m"]:
            i["y"] = y + i["h"] / 2
            y += i["h"] + gap
        placed.append(cl)
    # clamp bottom-up then top-down
    flat = [i for cl in placed for i in cl["m"]]
    flat.sort(key=lambda i: i["y"])
    prev = None
    for i in flat:
        mn = lo + i["h"] / 2
        if prev is not None:
            mn = max(mn, prev["y"] + prev["h"] / 2 + gap + i["h"] / 2)
        i["y"] = max(i["y"], mn)
        prev = i
    nxt = None
    for i in reversed(flat):
        mx = hi - i["h"] / 2
        if nxt is not None:
            mx = min(mx, nxt["y"] - nxt["h"] / 2 - gap - i["h"] / 2)
        i["y"] = min(i["y"], mx)
        nxt = i
# ======================================================================
# VISIO (.vsdx) READ + WRITE
# ======================================================================
from lxml import etree

V_NS = "http://schemas.microsoft.com/office/visio/2012/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NSMAP = {"v": V_NS}


def q(tag: str) -> str:
    return "{%s}%s" % (V_NS, tag)


KIT_MASTER_NAMES = {"Box Call-out.51", "Feature Call-out ", "Single Metal Band.53",
                    "Repair Dimension", "Coating D", "GPS Call-out.72"}
KIT_TEXT_RE = re.compile(r"XXX\+XX|XX\.XXXXX|^\s*Ht\s*#|^\s*0\.00\s*feet", re.I | re.M)


@dataclass
class AsBuilt:
    dig: str = ""
    line_name: str = ""
    segment: str = ""
    line_id: str = ""
    aid: str = ""
    us_id: str = ""
    us_sta: Optional[int] = None
    ds_id: str = ""
    ds_sta: Optional[int] = None
    county: str = ""
    cx: float = PIPE_CX
    y_us: float = US_Y0
    y_ds: float = DS_Y0
    warnings: List[str] = field(default_factory=list)

    @property
    def joint_len(self) -> Optional[float]:
        if self.us_sta is not None and self.ds_sta is not None:
            return float(self.ds_sta - self.us_sta)
        return None


class Pkg:
    """A .vsdx kept as an ordered {name: bytes} dict."""

    def __init__(self, data: bytes):
        self.files: Dict[str, bytes] = {}
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                self.files[n] = z.read(n)
        self.page_name = self._page_name()

    def _page_name(self) -> str:
        try:
            rels = etree.fromstring(self.files["visio/pages/_rels/pages.xml.rels"])
            pages = etree.fromstring(self.files["visio/pages/pages.xml"])
            rid = pages.find(q("Page")).find(q("Rel")).get("{%s}id" % R_NS)
            for r in rels:
                if r.get("Id") == rid:
                    return "visio/pages/" + r.get("Target")
        except Exception:
            pass
        return "visio/pages/page1.xml"

    def page_tree(self):
        return etree.fromstring(self.files[self.page_name])

    def master_names(self) -> Dict[str, str]:
        try:
            root = etree.fromstring(self.files["visio/masters/masters.xml"])
            return {m.get("ID"): (m.get("NameU") or m.get("Name") or "") for m in root.findall(q("Master"))}
        except Exception:
            return {}

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            names = list(self.files)
            if "[Content_Types].xml" in names:
                names.remove("[Content_Types].xml")
                names.insert(0, "[Content_Types].xml")
            for n in names:
                z.writestr(n, self.files[n])
        return buf.getvalue()


def shape_text(sh) -> str:
    t = sh.find(q("Text"))
    return "" if t is None else "".join(t.itertext()).replace("\r", "")


def cell_v(sh, name):
    c = sh.find(q("Cell") + "[@N='%s']" % name)
    return None if c is None else c.get("V")


def cell_f(sh, name):
    v = cell_v(sh, name)
    try:
        return float(v)
    except Exception:
        return None


def _top_shapes(root):
    return root.find(q("Shapes")).findall(q("Shape"))


def parse_asbuilt(data: bytes) -> Tuple[AsBuilt, Pkg]:
    pkg = Pkg(data)
    root = pkg.page_tree()
    names = pkg.master_names()
    info = AsBuilt()
    for sh in _top_shapes(root):
        txt = shape_text(sh)
        mname = names.get(sh.get("Master") or "", "")
        flat = re.sub(r"\s+", " ", txt)
        if re.search(r"\b(U/S|D/S)\s*Weld", flat, re.I):
            side = "us" if re.search(r"U/S\s*Weld", flat, re.I) else "ds"
            sta = parse_station(flat)
            rest = _STA_RE.sub(" ", flat)
            ids = re.findall(r"(?<![\d+])(\d{4,9})(?![\d+])", rest)
            wid = ids[0] if ids else ""
            y = cell_f(sh, "BeginY") or cell_f(sh, "PinY")
            if side == "us":
                info.us_sta, info.us_id = sta, wid
                if y:
                    info.y_us = y
            else:
                info.ds_sta, info.ds_id = sta, wid
                if y:
                    info.y_ds = y
        elif re.search(r"\bdig\s*#", flat, re.I) and ("Pipeline description" in mname or "AID" in flat.upper() or "ASMT" in flat.upper() or "LID" in flat.upper()):
            lines = [l.strip() for l in txt.split("\n") if l.strip()]
            if lines:
                info.line_name = lines[0]
            if len(lines) > 1:
                info.segment = lines[1]
            m = re.search(r"dig\s*#?\s*([0-9]+\s*[A-Za-z]?)", flat, re.I)
            if m:
                info.dig = re.sub(r"\s+", "", m.group(1)).upper()
            m = re.search(r"(?:Line\s*ID|LID)\s*:?\s*([0-9][0-9\-]*)", flat, re.I)
            if m:
                info.line_id = m.group(1)
            m = re.search(r"(?:ASMT\s*ID|AID)\s*:?\s*(\d+)", flat, re.I)
            if m:
                info.aid = m.group(1)
        elif "Legal Desc" in flat or "Tract Number" in flat:
            m = re.search(r"County,?\s*State:\s*(.*?)\s*Legal Desc", flat)
            if m:
                info.county = m.group(1).strip()
        elif mname == "Pipeline" or sh.get("NameU") == "Pipeline":
            x = cell_f(sh, "PinX")
            if x:
                info.cx = x
    if not info.dig:
        info.warnings.append("Could not read the dig number from the title block.")
    if info.us_sta is None:
        info.warnings.append("Could not read the U/S weld stationing.")
    return info, pkg


# ----------------------------------------------------------------------
# shape XML builders (plain shapes - no master dependency)
# ----------------------------------------------------------------------
def _n(x) -> str:
    return repr(round(float(x), 5))


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _text_xml(lines: List[str]) -> str:
    body = "\r\n".join(_esc(l) for l in lines) + "\r\n"
    return f"<Text><cp IX='0'/><pp IX='0'/>{body}</Text>"


def _char_sec(size_pt: float, style: int, color: str) -> str:
    return (f"<Section N='Character'><Row IX='0'><Cell N='Font' V='Calibri'/>"
            f"<Cell N='Color' V='{color}'/><Cell N='Style' V='{style}'/>"
            f"<Cell N='Size' V='{_n(size_pt / 72.0)}' U='PT'/></Row></Section>")


def _para_sec(halign: int) -> str:
    return (f"<Section N='Paragraph'><Row IX='0'><Cell N='HorzAlign' V='{halign}'/>"
            f"<Cell N='SpLine' V='-1' U='PT'/></Row></Section>")


class Builder:
    def __init__(self, start_id: int):
        self.next = start_id
        self.xml: List[str] = []

    def nid(self) -> int:
        self.next += 1
        return self.next

    # --- plain rectangle (optionally with text), unrotated ---------------
    def box(self, name, cx, cy, w, h, lines=None, size=8, style=1, color="#000000",
            halign=2, fill="#FFFFFF", border=True, line_w=0.0069, line_color="#000000", margin=0.05):
        sid = self.nid()
        nofill = 0 if fill else 1
        s = (f"<Shape ID='{sid}' NameU='{_esc(name)}' Name='{_esc(name)}' Type='Shape' LineStyle='3' FillStyle='3' TextStyle='3'>"
             f"<Cell N='PinX' V='{_n(cx)}'/><Cell N='PinY' V='{_n(cy)}'/><Cell N='Width' V='{_n(w)}'/><Cell N='Height' V='{_n(h)}'/>"
             f"<Cell N='LocPinX' V='{_n(w / 2)}'/><Cell N='LocPinY' V='{_n(h / 2)}'/><Cell N='Angle' V='0'/>"
             f"<Cell N='FlipX' V='0'/><Cell N='FlipY' V='0'/><Cell N='ResizeMode' V='0'/>"
             f"<Cell N='LineWeight' V='{line_w}'/><Cell N='LineColor' V='{line_color}'/><Cell N='LinePattern' V='1'/>"
             f"<Cell N='FillForegnd' V='{fill or '#FFFFFF'}'/><Cell N='FillPattern' V='{1 if fill else 0}'/>"
             f"<Cell N='LeftMargin' V='{margin}'/><Cell N='RightMargin' V='{margin}'/><Cell N='TopMargin' V='0.02'/><Cell N='BottomMargin' V='0.02'/>")
        if lines:
            s += _char_sec(size, style, color) + _para_sec(halign)
        s += (f"<Section N='Geometry' IX='0'><Cell N='NoFill' V='{nofill}'/><Cell N='NoLine' V='{0 if border else 1}'/>"
              f"<Cell N='NoShow' V='0'/><Cell N='NoSnap' V='0'/><Cell N='NoQuickDrag' V='0'/>"
              f"<Row T='MoveTo' IX='1'><Cell N='X' V='0'/><Cell N='Y' V='0'/></Row>"
              f"<Row T='LineTo' IX='2'><Cell N='X' V='{_n(w)}'/><Cell N='Y' V='0'/></Row>"
              f"<Row T='LineTo' IX='3'><Cell N='X' V='{_n(w)}'/><Cell N='Y' V='{_n(h)}'/></Row>"
              f"<Row T='LineTo' IX='4'><Cell N='X' V='0'/><Cell N='Y' V='{_n(h)}'/></Row>"
              f"<Row T='LineTo' IX='5'><Cell N='X' V='0'/><Cell N='Y' V='0'/></Row></Section>")
        if lines:
            s += _text_xml(lines)
        s += "</Shape>"
        self.xml.append(s)
        return sid

    # --- straight line, optional arrowheads ------------------------------
    def line(self, name, x1, y1, x2, y2, color="#000000", weight=0.0069, arrows=False, arrow_size=1):
        sid = self.nid()
        x0, y0 = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        s = (f"<Shape ID='{sid}' NameU='{_esc(name)}' Name='{_esc(name)}' Type='Shape' LineStyle='3' FillStyle='3' TextStyle='3'>"
             f"<Cell N='PinX' V='{_n(x0)}'/><Cell N='PinY' V='{_n(y0)}'/><Cell N='Width' V='{_n(w)}'/><Cell N='Height' V='{_n(h)}'/>"
             f"<Cell N='LocPinX' V='0'/><Cell N='LocPinY' V='0'/><Cell N='Angle' V='0'/>"
             f"<Cell N='FlipX' V='0'/><Cell N='FlipY' V='0'/><Cell N='ResizeMode' V='0'/>"
             f"<Cell N='LineWeight' V='{weight}'/><Cell N='LineColor' V='{color}'/><Cell N='LinePattern' V='1'/>")
        if arrows:
            s += (f"<Cell N='BeginArrow' V='4'/><Cell N='BeginArrowSize' V='{arrow_size}'/>"
                  f"<Cell N='EndArrow' V='4'/><Cell N='EndArrowSize' V='{arrow_size}'/>")
        s += (f"<Cell N='FillPattern' V='0'/>"
              f"<Section N='Geometry' IX='0'><Cell N='NoFill' V='1'/><Cell N='NoLine' V='0'/><Cell N='NoShow' V='0'/>"
              f"<Cell N='NoSnap' V='0'/><Cell N='NoQuickDrag' V='0'/>"
              f"<Row T='MoveTo' IX='1'><Cell N='X' V='{_n(x1 - x0)}'/><Cell N='Y' V='{_n(y1 - y0)}'/></Row>"
              f"<Row T='LineTo' IX='2'><Cell N='X' V='{_n(x2 - x0)}'/><Cell N='Y' V='{_n(y2 - y0)}'/></Row></Section></Shape>")
        self.xml.append(s)
        return sid

    # --- vertical cylinder (coating / sleeve / band), same recipe as the
    #     template's own Pipe/Sleeve/Band masters (rotated -90deg + gradient)
    def cylinder(self, name, cx, y_bot, y_top, diam, edge, centre, lines=None, size=11, style=0,
                 line_w=None):
        sid = self.nid()
        L = abs(y_top - y_bot)
        cy = (y_top + y_bot) / 2
        s = (f"<Shape ID='{sid}' NameU='{_esc(name)}' Name='{_esc(name)}' Type='Shape' LineStyle='3' FillStyle='3' TextStyle='3'>"
             f"<Cell N='PinX' V='{_n(cx)}'/><Cell N='PinY' V='{_n(cy)}'/><Cell N='Width' V='{_n(L)}'/><Cell N='Height' V='{_n(diam)}'/>"
             f"<Cell N='LocPinX' V='{_n(L / 2)}'/><Cell N='LocPinY' V='{_n(diam / 2)}'/><Cell N='Angle' V='-1.5707963267949'/>"
             f"<Cell N='FlipX' V='0'/><Cell N='FlipY' V='0'/><Cell N='ResizeMode' V='0'/>"
             f"<Cell N='FillForegnd' V='{centre}'/><Cell N='FillBkgnd' V='{edge}'/><Cell N='FillPattern' V='29'/>"
             f"<Cell N='FillGradientAngle' V='1.5707963267949' U='DEG'/><Cell N='FillGradientEnabled' V='1'/>")
        if line_w:
            s += f"<Cell N='LineWeight' V='{line_w}'/>"
        s += (f"<Section N='FillGradient'>"
              f"<Row IX='0'><Cell N='GradientStopColor' V='{edge}'/><Cell N='GradientStopPosition' V='0.01'/></Row>"
              f"<Row IX='1'><Cell N='GradientStopColor' V='{centre}'/><Cell N='GradientStopColorTrans' V='0'/><Cell N='GradientStopPosition' V='0.5'/></Row>"
              f"<Row IX='2'><Cell N='GradientStopColor' V='{edge}'/><Cell N='GradientStopColorTrans' V='0'/><Cell N='GradientStopPosition' V='1'/></Row>"
              f"</Section>")
        if lines:
            s += _char_sec(size, style, "#000000") + _para_sec(1)
        s += (f"<Section N='Geometry' IX='0'><Cell N='NoFill' V='0'/><Cell N='NoLine' V='0'/><Cell N='NoShow' V='0'/>"
              f"<Cell N='NoSnap' V='0'/><Cell N='NoQuickDrag' V='0'/>"
              f"<Row T='MoveTo' IX='1'><Cell N='X' V='0'/><Cell N='Y' V='0'/></Row>"
              f"<Row T='LineTo' IX='2'><Cell N='X' V='{_n(L)}'/><Cell N='Y' V='0'/></Row>"
              f"<Row T='LineTo' IX='3'><Cell N='X' V='{_n(L)}'/><Cell N='Y' V='{_n(diam)}'/></Row>"
              f"<Row T='LineTo' IX='4'><Cell N='X' V='0'/><Cell N='Y' V='{_n(diam)}'/></Row>"
              f"<Row T='LineTo' IX='5'><Cell N='X' V='0'/><Cell N='Y' V='0'/></Row></Section>")
        if lines:
            s += _text_xml(lines)
        s += "</Shape>"
        self.xml.append(s)
        return sid

    def wrap(self, name, cx, y_bot, y_top, diam):
        """Hatched 'Wrap Coating' look from the stencil (FillPattern 5, black on #7F7F7F)."""
        sid = self.nid()
        L = abs(y_top - y_bot)
        cy = (y_top + y_bot) / 2
        s = (f"<Shape ID='{sid}' NameU='{_esc(name)}' Name='{_esc(name)}' Type='Shape' LineStyle='3' FillStyle='3' TextStyle='3'>"
             f"<Cell N='PinX' V='{_n(cx)}'/><Cell N='PinY' V='{_n(cy)}'/><Cell N='Width' V='{_n(L)}'/><Cell N='Height' V='{_n(diam)}'/>"
             f"<Cell N='LocPinX' V='{_n(L / 2)}'/><Cell N='LocPinY' V='{_n(diam / 2)}'/><Cell N='Angle' V='-1.5707963267949'/>"
             f"<Cell N='FlipX' V='0'/><Cell N='FlipY' V='0'/><Cell N='ResizeMode' V='0'/>"
             f"<Cell N='FillForegnd' V='#000000'/><Cell N='FillBkgnd' V='#7F7F7F'/><Cell N='FillPattern' V='5'/>"
             f"<Section N='Geometry' IX='0'><Cell N='NoFill' V='0'/><Cell N='NoLine' V='0'/><Cell N='NoShow' V='0'/>"
             f"<Cell N='NoSnap' V='0'/><Cell N='NoQuickDrag' V='0'/>"
             f"<Row T='MoveTo' IX='1'><Cell N='X' V='0'/><Cell N='Y' V='0'/></Row>"
             f"<Row T='LineTo' IX='2'><Cell N='X' V='{_n(L)}'/><Cell N='Y' V='0'/></Row>"
             f"<Row T='LineTo' IX='3'><Cell N='X' V='{_n(L)}'/><Cell N='Y' V='{_n(diam)}'/></Row>"
             f"<Row T='LineTo' IX='4'><Cell N='X' V='0'/><Cell N='Y' V='{_n(diam)}'/></Row>"
             f"<Row T='LineTo' IX='5'><Cell N='X' V='0'/><Cell N='Y' V='0'/></Row></Section></Shape>")
        self.xml.append(s)
        return sid

    def elements(self):
        out = []
        for x in self.xml:
            out.append(etree.fromstring(f"<Wrapper xmlns='{V_NS}' xmlns:r='{R_NS}'>{x}</Wrapper>")[0])
        return out


def est_width(text: str, size_pt: float = 8, bold: bool = True) -> float:
    """Rough Calibri width in inches."""
    return len(text) * size_pt * (0.47 if bold else 0.43) / 72.0


# ----------------------------------------------------------------------
# job-box text (D.O.C., prepared by, date)
# ----------------------------------------------------------------------
def set_job_field(text_el, label: str, value: str) -> bool:
    segs = []   # (element, kind) tails in order; text_el.text first
    segs.append((text_el, "text"))
    for ch in text_el:
        segs.append((ch, "tail"))

    def get(i):
        el, k = segs[i]
        return (el.text if k == "text" else el.tail) or ""

    def put(i, v):
        el, k = segs[i]
        if k == "text":
            el.text = v
        else:
            el.tail = v

    li = None
    for i in range(len(segs)):
        if label.lower() in get(i).lower():
            li = i
            break
    if li is None:
        return False
    rng = []
    k = li + 1
    while k < len(segs):
        rng.append(k)
        if "\n" in get(k):
            break
        k += 1
    if not rng:
        return False
    # the label's own tail may already contain the value after a colon
    target = None
    for k in rng:
        t = get(k).strip()
        if t and t != ":":
            target = k
            break
    if target is None:
        target = rng[-1]
    old = get(target)
    lead = re.match(r"[ \t]*", old).group(0)
    nl = "\n" if "\n" in old else ""
    put(target, lead + value + nl)
    # blank any other leftover non-space value tails on the same line
    for k in rng:
        if k != target:
            t = get(k)
            if t.strip() not in ("", ":"):
                put(k, "\n" if "\n" in t else "")
    return True
# ======================================================================
# BUILD THE COMPLETED AS-BUILT
# ======================================================================
TAG = "AB:"          # every shape this app draws is named "AB: ..."


def _wrap_sleeve(prefix: str, stype: str, width: float) -> List[str]:
    a, b = begin_end_labels(stype)
    full = f"{prefix} {a} {b}".strip()
    if est_width(full) <= width - 0.10 or not b:
        return [full]
    return [f"{prefix} {a}".strip(), b]


def _collect_ids(root) -> int:
    mx = 0
    for el in root.iter(q("Shape")):
        try:
            mx = max(mx, int(el.get("ID")))
        except Exception:
            pass
    return mx


def _set_cell(sh, name, value, formula_keep=True):
    c = sh.find(q("Cell") + "[@N='%s']" % name)
    if c is None:
        c = etree.SubElement(sh, q("Cell"))
        c.set("N", name)
    c.set("V", _n(value))


def build_asbuilt(data: bytes, rep: RepairIn) -> Tuple[bytes, dict]:
    info, pkg = parse_asbuilt(data)
    if info.us_sta is None:
        raise ValueError("The as-built has no readable U/S weld stationing.")
    root = pkg.page_tree()
    shapes_el = root.find(q("Shapes"))
    names = pkg.master_names()
    notes: List[str] = []

    # --- 1. clear the template kit and anything a previous run drew -----
    for sh in list(shapes_el.findall(q("Shape"))):
        txt = shape_text(sh)
        flat = re.sub(r"\s+", " ", txt)
        mname = names.get(sh.get("Master") or "", "")
        protected = re.search(r"\b(U/S|D/S)\s*Weld|Legal Desc|\bdig\s*#", flat, re.I)
        if (sh.get("NameU") or "").startswith(TAG):
            shapes_el.remove(sh)
        elif mname in KIT_MASTER_NAMES or (KIT_TEXT_RE.search(txt) and not protected):
            shapes_el.remove(sh)

    lay = compute_layout(rep)
    cx = info.cx
    us_sta = info.us_sta

    def sta(d):
        return fmt_station(station_at(us_sta, d))

    # --- 2. pipe + U/S weld move up when the coating starts upstream ----
    delta = lay.y_us - info.y_us
    for sh in shapes_el.findall(q("Shape")):
        mname = names.get(sh.get("Master") or "", "")
        txt = shape_text(sh)
        if sh.get("NameU") == "Pipeline" or mname == "Pipeline":
            L = lay.pipe_top - lay.pipe_bottom
            _set_cell(sh, "PinY", (lay.pipe_top + lay.pipe_bottom) / 2)
            _set_cell(sh, "Width", L)
            _set_cell(sh, "LocPinX", L / 2)
            for row in sh.iterfind(".//" + q("Row")):
                if row.get("T") == "LineTo" and row.get("IX") in ("2", "3"):
                    for c in row.findall(q("Cell")):
                        if c.get("N") == "X":
                            c.set("V", _n(L))
        elif sh.get("Type") == "Group" and (cell_f(sh, "PinY") or 99) < 4.0 and abs((cell_f(sh, "PinY") or 0) - info.y_us) < 0.6:
            _set_cell(sh, "PinY", cell_f(sh, "PinY") + delta)
        elif re.search(r"U/S\s*Weld", txt):
            for n in ("PinY", "BeginY", "EndY"):
                v = cell_f(sh, n)
                if v is not None:
                    _set_cell(sh, n, v + delta)

    # --- 3. the new drawing ------------------------------------------
    start = _collect_ids(root)
    bz = Builder(start + 1000)     # behind: coating, sleeves, bands
    bt = Builder(start + 2000)     # on top: leaders, callouts, dimensions

    # existing coating (everywhere the repair coating is not) - drawn first, underneath
    if rep.existing == "Coal Tar":
        bz.cylinder(f"{TAG} Existing Coal Tar", cx, lay.pipe_bottom, lay.pipe_top, 2 * PIPE_R + 0.02,
                    "#000000", brighten("#000000"))
    elif rep.existing == "Polyken Tape":
        bz.wrap(f"{TAG} Existing Polyken Tape", cx, lay.pipe_bottom, lay.pipe_top, 2 * PIPE_R + 0.02)
    # coating
    edge = rep.coating_color.upper()
    cuts = cut_list(rep)
    for k, (a, b) in enumerate(cuts, 1):
        bz.cylinder(f"{TAG} Coating {k}", cx, lay.y(a), lay.y(b), 2 * COAT_R, edge, brighten(edge))
    # sleeves
    sleeves = sorted(rep.sleeves, key=lambda s: s.begin)
    for i, s in enumerate(sleeves, 1):
        lines = [s.type] + ([f"HT# {s.heat.strip()}"] if s.heat.strip() else [])
        bz.cylinder(f"{TAG} Sleeve {i}", cx, lay.y(s.begin), lay.y(s.end), 2 * SLEEVE_R,
                    "#7F7F7F", "#F2F2F2", lines=lines, size=11)
    # bands
    bands = band_positions(rep, lay)
    for y, far in bands:
        bz.cylinder(f"{TAG} Metal Band", cx, y - BAND_THICK / 2, y + BAND_THICK / 2, 0.539, "#000000", "#F2F2F2")

    # left-hand boxes
    items = []

    def add_item(kind, d, x_attach, lines):
        W = max(1.086, max(est_width(l) for l in lines) + 0.14)
        items.append(dict(kind=kind, y_feat=lay.y(d), x_att=x_attach, lines=lines, W=W,
                          h=len(lines) * LINE_H + 0.111))

    for k, (a, b) in enumerate(cuts, 1):
        tag = f" {k}" if len(cuts) > 1 else ""
        add_item(f"cb{k}", a, cx - COAT_R, [f"Begin Coating{tag}", rep.coating_name, sta(a)])
        add_item(f"ce{k}", b, cx - COAT_R, [f"End Coating{tag}", rep.coating_name, sta(b)])
    for i, s in enumerate(sleeves, 1):
        add_item(f"sb{i}", s.begin, cx - SLEEVE_R, _wrap_sleeve("Begin", s.type, 1.086) + [sta(s.begin)])
        add_item(f"se{i}", s.end, cx - SLEEVE_R, _wrap_sleeve("End", s.type, 1.086) + [sta(s.end)])
    if rep.existing:
        ye = (lay.y(rep.ce) + lay.y_ds) / 2
        items.append(dict(kind="ex", y_feat=ye, x_att=cx - PIPE_R - 0.01,
                          lines=["Existing Coating", rep.existing], W=1.086, h=2 * LINE_H + 0.111))
    pack_boxes(items, lo=0.55, hi=9.05)
    for it in items:
        bt.line(f"{TAG} Leader {it['kind']}", BOX_RIGHT, it["y"], it["x_att"], it["y_feat"])
    for it in items:
        bt.box(f"{TAG} {it['kind']} callout", BOX_RIGHT - it["W"] / 2, it["y"], it["W"], it["h"],
               lines=it["lines"], size=8, style=1, halign=2)

    # GPS callouts (centre of repair)
    gps_targets = []
    if sleeves:
        for i, s in enumerate(sleeves, 1):
            gps_targets.append((i, s.gps, (lay.y(s.begin) + lay.y(s.end)) / 2))
    else:
        gps_targets.append((0, rep.center_gps, (lay.y(rep.cb) + lay.y(rep.ce)) / 2))
    for i, g, yc in gps_targets:
        if not g:
            notes.append(f"No GPS chosen for {'sleeve ' + str(i) if i else 'the repair'} - callout left off.")
            continue
        xr = 2.95
        bt.line(f"{TAG} Leader GPS {i}", xr + 0.03, yc, cx - SLEEVE_R, yc)
        bt.box(f"{TAG} GPS {i}", xr - 0.55, yc, 1.10, 0.30, lines=[f"{g[0]:.8f}", f"{g[1]:.8f}"],
               size=8, style=5, color="#0000FF", halign=2, fill=None, border=False)

    # metal band label (points at the outermost band below the repair)
    if bands:
        far = [b for b in bands if b[1]] or bands
        yb = min(b[0] for b in far)
        yt = max(0.95, yb - 0.42)
        bt.line(f"{TAG} Leader Metal Band", 2.62, yt + 0.07, cx - 0.27, yb)
        bt.box(f"{TAG} Metal Band label", 2.20, yt, 0.80, 0.18, lines=["Metal Band"], size=8, style=1,
               halign=2, fill=None, border=False)

    # dimensions ---------------------------------------------------------
    dims = []
    if abs(cuts[0][0]) > 1e-6:
        dims.append(dict(a=0.0, b=cuts[0][0], blue=True, col=0))
    for s in sleeves:
        dims.append(dict(a=s.begin, b=s.end, blue=False, col=0, length=True))
    dims.append(dict(a=0.0, b=cuts[0][1], blue=True, col=1))
    rest = []
    # first cut keeps the fixed blue columns above; extra cuts join the sorted black columns
    for (a, b) in cuts[1:]:
        if abs(a) > 1e-6:
            rest.append(dict(a=0.0, b=a, blue=True))
        rest.append(dict(a=0.0, b=b, blue=True))
    for s in sleeves:
        if abs(s.begin) > 1e-6:
            rest.append(dict(a=0.0, b=s.begin, blue=False))
        rest.append(dict(a=0.0, b=s.end, blue=False))
    rest.sort(key=lambda d: abs(d["b"]))
    for k, d in enumerate(rest):
        d["col"] = 2 + k
    dims += rest
    used = sorted({d["col"] for d in dims})
    colidx = {c: i for i, c in enumerate(used)}
    x0 = cx + SLEEVE_R
    first = x0 + 0.36
    ncol = len(used)
    step = 0.34 if ncol <= 1 else min(0.34, (DIM_X_MAX - first) / (ncol - 1))
    placed: List[Tuple[float, float]] = []
    for d in sorted(dims, key=lambda d: d["col"]):
        x = first + colidx[d["col"]] * step
        ya, yb2 = lay.y(d["a"]), lay.y(d["b"])
        ytop, ybot = max(ya, yb2), min(ya, yb2)
        color = "#00B0F0" if d["blue"] else "#000000"
        val = abs(d["b"] - d["a"]) if d.get("length") else d["b"]
        text = f"{val:.2f} feet"
        for y in (ya, yb2):
            bt.line(f"{TAG} Dim ext", x0 + 0.02, y, x + 0.07, y, color=color if d["blue"] else "#404040", weight=0.0035)
        bt.line(f"{TAG} Dim {text}", x, ya, x, yb2, color=color, weight=0.0069, arrows=True)
        tw = est_width(text) + 0.08
        span = ytop - ybot
        cands = [0.5, 0.35, 0.65, 0.2, 0.8, 0.12, 0.88]
        ychoice = None
        for f in cands:
            y = ybot + span * f
            if span * f < 0.10 or span * (1 - f) < 0.10:
                continue
            if all(abs(x - px) > (tw + 0.30) or abs(y - py) > 0.19 for px, py in placed):
                ychoice = y
                break
        if ychoice is None:
            ychoice = ybot + span * 0.5
        placed.append((x, ychoice))
        bt.box(f"{TAG} Dim label {text}", x, ychoice, tw, 0.15, lines=[text], size=8, style=1, color=color,
               halign=1, fill="#FFFFFF", border=False, margin=0.01)

    # insert into the page ----------------------------------------------
    pipe_idx = None
    for i, sh in enumerate(shapes_el):
        if sh.get("NameU") == "Pipeline" or names.get(sh.get("Master") or "", "") == "Pipeline":
            pipe_idx = i
            break
    at = (pipe_idx + 1) if pipe_idx is not None else len(shapes_el)
    for k, el in enumerate(bz.elements()):
        shapes_el.insert(at + k, el)
    for el in bt.elements():
        shapes_el.append(el)

    # --- 4. job box ----------------------------------------------------
    jb = None
    for sh in shapes_el.findall(q("Shape")):
        if "Legal Desc" in shape_text(sh):
            jb = sh
    if jb is None or jb.find(q("Text")) is None:
        notes.append("The job box on this file has no text of its own - D.O.C./name/date not written.")
    else:
        t = jb.find(q("Text"))
        if rep.depth_text:
            set_job_field(t, "D.O.C", rep.depth_text)
        if rep.name:
            set_job_field(t, "Prepared by", rep.name)
        if rep.date_text:
            set_job_field(t, "Date:", rep.date_text)
        # long entries (e.g. a long alignment-sheet name) wrap and push the last
        # line out of the box; shrink the box font a little so everything fits
        txt = "".join(t.itertext())
        if max((len(x) for x in txt.replace("\r", "").split("\n")), default=0) > 28:
            for sec in jb.findall(q("Section")):
                if sec.get("N") == "Character":
                    for cell in sec.iter(q("Cell")):
                        if cell.get("N") == "Size":
                            try:
                                cell.set("V", repr(float(cell.get("V")) * 0.9))
                            except (TypeError, ValueError):
                                pass
                            if cell.get("F"):
                                del cell.attrib["F"]

    # --- 5. tidy the package -------------------------------------------
    out = etree.tostring(root, xml_declaration=True, encoding="utf-8").decode("utf-8")
    out = re.sub(r"<Text>.*?</Text>", lambda m: m.group(0).replace("\n", "\r\n"), out, flags=re.S)
    pkg.files[pkg.page_name] = out.encode("utf-8")
    _strip_comments_and_thumbnail(pkg)
    return pkg.to_bytes(), dict(info=info, lay=lay, notes=notes, items=items, dims=dims)


def _strip_comments_and_thumbnail(pkg: Pkg):
    f = pkg.files
    f.pop("visio/comments.xml", None)
    p = "visio/_rels/document.xml.rels"
    if p in f:
        root = etree.fromstring(f[p])
        for r in list(root):
            if r.get("Target", "").endswith("comments.xml"):
                root.remove(r)
        f[p] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    p = "[Content_Types].xml"
    if p in f:
        root = etree.fromstring(f[p])
        for o in list(root):
            if o.get("PartName", "").endswith("comments.xml"):
                root.remove(o)
            if o.get("PartName", "").endswith("thumbnail.emf"):
                root.remove(o)
        f[p] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    if "docProps/thumbnail.emf" in f:
        f.pop("docProps/thumbnail.emf")
        p = "_rels/.rels"
        root = etree.fromstring(f[p])
        for r in list(root):
            if r.get("Target", "").endswith("thumbnail.emf"):
                root.remove(r)
        f[p] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
# ---------------------------------------------------------------------------
# Excavation Survey Report fill (new AcroForm template + legacy XFA reports)
# ---------------------------------------------------------------------------
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, BooleanObject, TextStringObject


@dataclass
class WeldRow:
    number: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    elev: Optional[float] = None


def _fmt(v, nd):
    return "" if v is None else f"{v:.{nd}f}"


def survey_mode(pdf: bytes) -> str:
    r = PdfReader(io.BytesIO(pdf))
    f = r.get_fields() or {}
    if "weldNum_1" in f:
        return "acroform"
    af = r.trailer["/Root"].get("/AcroForm")
    if af is not None and "/XFA" in af:
        return "xfa"
    return "unknown"


def fill_survey(pdf: bytes, ab: AsBuilt, welds: List[WeldRow], name: str, date_text: str) -> Tuple[bytes, List[str]]:
    """Fill weld/GPS rows + header + submitted by/date. Returns (pdf bytes, notes)."""
    notes: List[str] = []
    mode = survey_mode(pdf)
    welds = [w for w in welds if w.number or w.lat is not None]
    if mode == "acroform":
        vals = {}
        # header: only fill what is empty in the template
        r0 = PdfReader(io.BytesIO(pdf))
        cur = {k: (v.get("/V") or "") for k, v in (r0.get_fields() or {}).items()}
        for k, v in (("pipelineName", ab.line_name), ("pipelineNumber", ab.line_id),
                     ("digNumber", ab.dig), ("assessmentID", ab.aid),
                     ("assessmentSegmentName", ab.segment)):
            if v and not str(cur.get(k, "")).strip():
                vals[k] = v
        for i, w in enumerate(welds[:5], 1):
            vals[f"weldNum_{i}"] = w.number
            vals[f"weldLat_{i}"] = _fmt(w.lat, 8)
            vals[f"weldLon_{i}"] = _fmt(w.lon, 8)
            vals[f"weldElev_{i}"] = _fmt(w.elev, 2)
        if len(welds) > 5:
            notes.append("Only the first 5 welds fit on the report.")
        if name:
            vals["submittedBy"] = name
        if date_text:
            vals["submittalDate"] = date_text
        w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
        for page in w.pages:
            w.update_page_form_field_values(page, vals, auto_regenerate=False)
        w._root_object["/AcroForm"][NameObject("/NeedAppearances")] = BooleanObject(True)
        out = io.BytesIO()
        w.write(out)
        return out.getvalue(), notes
    if mode == "xfa":
        r = PdfReader(io.BytesIO(pdf))
        xfa = r.trailer["/Root"]["/AcroForm"]["/XFA"]
        idx = None
        for k in range(0, len(xfa), 2):
            if xfa[k] == "datasets":
                idx = k + 1
        stream = xfa[idx].get_object()
        root = etree.fromstring(stream.get_data())
        data = root.find("{http://www.xfa.org/schema/xfa-data/1.0/}data")
        rep = data[0]

        def find(tag, parent=rep):
            for e in parent.iter():
                if etree.QName(e).localname == tag:
                    return e
            return None

        rows = [e for e in rep.iter() if etree.QName(e).localname == "WeldNumber"]
        for e, w in zip(rows, welds):
            for tag, val in (("pipeconnectorNumber", w.number), ("pipeconnectorLatitude", _fmt(w.lat, 8)),
                             ("pipeconnectorLongitude", _fmt(w.lon, 8)), ("pipeconnectorElevationTopOfPipe", _fmt(w.elev, 2))):
                el = find(tag, e)
                if el is not None:
                    el.text = val
        if len(welds) > len(rows):
            notes.append(f"Only {len(rows)} weld rows exist on this report.")
        for tag, val in (("excavationSurveyReportSubmitBy", name), ("excavationSurveyReportSubmitDate", date_text)):
            el = find(tag)
            if el is not None and val:
                el.text = val
        stream.set_data(etree.tostring(root))
        w = PdfWriter(clone_from=r)
        out = io.BytesIO()
        w.write(out)
        return out.getvalue(), notes
    raise ValueError("This PDF is not a recognised Excavation Survey Report (no AcroForm weld fields or XFA data).")
# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
import shutil
import subprocess
import tempfile
import os


def _shot_label(s: Shot) -> str:
    return f"{s.name.strip()}  |  {s.lat:.8f}, {s.lon:.8f}  |  {s.elev:.2f} ft  |  {s.created}"


def _pick(st, label, cands, key, default=None):
    """Multiselect of candidate shots -> list of Shots (averaged downstream)."""
    labels = [_shot_label(s) for s in cands]
    dflt = [_shot_label(s) for s in (default or [])]
    chosen = st.multiselect(label, labels, default=[d for d in dflt if d in labels], key=key)
    return [cands[labels.index(c)] for c in chosen]


def _gps_override(st, key, shots):
    """Return (lat, lon) from picked shots (average) or manual entry."""
    c = centroid(shots) if shots else None
    manual = st.text_input("…or type lat, lon manually", key=key + "_manual", placeholder="39.44850394, -93.94658221")
    if manual.strip():
        try:
            a, b = [float(x) for x in manual.replace(",", " ").split()[:2]]
            return (a, b)
        except ValueError:
            st.warning("Could not read that as 'lat, lon'.")
    if c:
        st.caption(f"Center of repair = average of {len(shots)} shot(s): {c[0]:.8f}, {c[1]:.8f}")
        return (round(c[0], 8), round(c[1], 8))
    return None


def preview_png(vsdx: bytes) -> Optional[bytes]:
    """Optional preview; only works where LibreOffice + pdftoppm exist."""
    if not shutil.which("soffice") or not shutil.which("pdftoppm"):
        return None
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "p.vsdx")
        open(p, "wb").write(vsdx)
        try:
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", d, p],
                           capture_output=True, timeout=120)
            subprocess.run(["pdftoppm", "-r", "80", "-png", "-singlefile", os.path.join(d, "p.pdf"), os.path.join(d, "p")],
                           capture_output=True, timeout=60)
            return open(os.path.join(d, "p.png"), "rb").read()
        except Exception:
            return None


def main():
    import streamlit as st
    st.set_page_config(page_title="Enterprise As-Built Completer", layout="wide")
    st.title("Enterprise As-Built + Excavation Survey Report")
    st.caption("Upload the as-built from the dig file generator, enter the repair, get the completed .vsdx and survey report.")

    c1, c2, c3 = st.columns(3)
    f_ab = c1.file_uploader("As-built (.vsdx) from the generator", type=["vsdx"])
    f_gps = c2.file_uploader("Raw GPS export (.csv)", type=["csv"])
    f_sr = c3.file_uploader("Excavation Survey Report (.pdf)", type=["pdf"])
    if not f_ab:
        st.info("Start by uploading the as-built .vsdx.")
        return
    ab_bytes = f_ab.getvalue()
    try:
        ab, _ = parse_asbuilt(ab_bytes)
    except Exception as e:  # noqa
        st.error(f"Could not read that as-built: {e}")
        return
    for w in ab.warnings:
        st.warning(w)
    jl = ab.joint_len
    st.success(f"Dig {ab.dig or '?'}  |  U/S weld {ab.us_id} @ {fmt_station(ab.us_sta) if ab.us_sta is not None else '?'}  |  "
               f"D/S weld {ab.ds_id} @ {fmt_station(ab.ds_sta) if ab.ds_sta is not None else '?'}"
               + (f"  |  joint {jl:.0f} ft" if jl else ""))

    shots, cands = [], []
    if f_gps:
        shots = load_shots(f_gps.getvalue())
        cands = classify_for_dig(shots, ab.dig)
        st.caption(f"{len(shots)} shots loaded; {len(cands)} match dig {ab.dig}.")
    pool = cands or shots   # fall back to every shot so the user can still pick

    st.header("Repair")
    a, b = st.columns(2)
    with a:
        names = list(DEFAULT_COATINGS) + ["Other..."]
        cn = st.selectbox("Coating type", names)
        if cn == "Other...":
            cn = st.text_input("Coating name", "")
            color = st.color_picker("Coating color", "#00B050")
        else:
            color = DEFAULT_COATINGS[cn]
            st.color_picker("Coating color (set by type)", color, disabled=True)
        ex = st.selectbox("Existing coating on pipe (everywhere the new coating isn't)", ["(none)"] + EXISTING_COATINGS)
        ncut = st.number_input("Number of tape cuts (coating lengths)", min_value=1, max_value=4, value=1, step=1)
        cuts = []
        for k in range(int(ncut)):
            t1, t2 = st.columns(2)
            ca = t1.number_input(f"Tape cut {k + 1} start (ft from U/S weld; negative = upstream)", value=0.0, step=0.05,
                                 format="%.2f", key=f"ca{k}")
            cz = t2.number_input(f"Tape cut {k + 1} end (ft from U/S weld)", value=0.0, step=0.05, format="%.2f", key=f"cz{k}")
            cuts.append((ca, cz))
        cb = min(c[0] for c in cuts)
        ce = max(c[1] for c in cuts)
    with b:
        nm = st.text_input("As-built prepared by (name)")
        dt = st.date_input("Survey date", value=date.today(), format="MM/DD/YYYY")
        d1, d2 = st.columns(2)
        ft = d1.number_input("Depth of cover - feet", min_value=0, value=0, step=1)
        inch = d2.number_input("inches", min_value=0, max_value=11, value=0, step=1)
    n = st.number_input("Number of sleeves", min_value=0, max_value=6, value=1, step=1)
    guess = suggest_sleeves(cands, int(n)) if cands else [[] for _ in range(int(n))]
    sleeves = []
    for i in range(int(n)):
        st.subheader(f"Sleeve {i + 1} (numbered upstream → downstream)")
        s1, s2, s3, s4 = st.columns(4)
        ty = s1.selectbox("Type", SLEEVE_TYPES, key=f"ty{i}")
        if ty == "Other...":
            ty = s1.text_input("Sleeve type name", key=f"tyo{i}")
        heat = s2.text_input("Heat #", key=f"ht{i}")
        bg = s3.number_input("Begin (ft from U/S weld)", value=0.0, step=0.05, format="%.2f", key=f"bg{i}")
        en = s4.number_input("End (ft from U/S weld)", value=0.0, step=0.05, format="%.2f", key=f"en{i}")
        gps = None
        if pool:
            pk = _pick(st, "GPS shot(s) for center of repair", pool, f"gp{i}", guess[i] if i < len(guess) else [])
            gps = _gps_override(st, f"gp{i}", pk)
        else:
            gps = _gps_override(st, f"gp{i}", [])
        sleeves.append(SleeveIn(ty, heat, bg, en, gps))
    center = None
    if n == 0:
        st.subheader("Center of repair")
        pk = _pick(st, "GPS shot(s)", pool, "gpc") if pool else []
        center = _gps_override(st, "gpc", pk)

    st.header("Survey report welds")
    us_shot = ds_shot = None
    use_ds = False
    if f_sr:
        wl = [s for s in pool if s.role == "weld"] or pool
        labels = ["(none)"] + [_shot_label(s) for s in wl]
        su, sd = suggest_weld(cands, "us"), suggest_weld(cands, "ds")
        w1, w2 = st.columns(2)
        pick_u = w1.selectbox("U/S weld shot", labels, index=(labels.index(_shot_label(su)) if su and _shot_label(su) in labels else 0))
        use_ds = w2.checkbox("Also list D/S weld on the report", value=False)
        pick_d = w2.selectbox("D/S weld shot", labels, index=(labels.index(_shot_label(sd)) if sd and _shot_label(sd) in labels else 0))
        us_shot = wl[labels.index(pick_u) - 1] if pick_u != "(none)" else None
        ds_shot = wl[labels.index(pick_d) - 1] if pick_d != "(none)" else None

    if st.button("Build files", type="primary"):
        st.session_state.pop("res", None)
        rep = RepairIn(coating_name=cn, coating_color=color, cb=cb, ce=ce, cuts=cuts, sleeves=sleeves, center_gps=center,
                       depth_text=(f"{int(ft)}' {int(inch):02d}\"" if (ft or inch) else ""),
                       name=nm, date_text=dt.strftime("%m/%d/%y"),
                       existing=("" if ex == "(none)" else ex))
        probs = validate_repair(rep, jl)
        for p in probs:
            st.error(p)
        if probs:
            return
        try:
            out, meta = build_asbuilt(ab_bytes, rep)
        except Exception as e:  # noqa
            st.exception(e)
            return
        res = dict(base=f"{ab.dig or 'Dig'}", vsdx=out, notes=list(meta["notes"]), pdf=None, png=preview_png(out))
        if f_sr:
            welds = []
            if us_shot:
                welds.append(WeldRow(ab.us_id, us_shot.lat, us_shot.lon, us_shot.elev))
            if use_ds and ds_shot:
                welds.append(WeldRow(ab.ds_id, ds_shot.lat, ds_shot.lon, ds_shot.elev))
            if not welds:
                res["notes"].append("No weld shot selected - survey report weld rows left blank.")
            try:
                pdf, notes = fill_survey(f_sr.getvalue(), ab, welds, nm, rep.date_text)
                res["pdf"] = pdf
                res["notes"] += notes
            except Exception as e:  # noqa
                res["notes"].append(f"Survey report not filled: {e}")
        st.session_state["res"] = res

    res = st.session_state.get("res")
    if not res:
        return
    base = res["base"]
    for nte in res["notes"]:
        st.warning(nte)
    vname = f"{base}_As-Built.vsdx"
    pname = f"ExcavationSurvey_Report_Dig_{base}.pdf"
    if res["pdf"]:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(vname, res["vsdx"])
            zf.writestr(pname, res["pdf"])
        st.download_button("Download both (as-built + survey report)", buf.getvalue(), file_name=f"Dig_{base}_Reports.zip",
                           mime="application/zip", type="primary")
        st.download_button("Survey report only (.pdf)", res["pdf"], file_name=pname, mime="application/pdf")
    st.download_button("As-built only (.vsdx)", res["vsdx"], file_name=vname, mime="application/vnd.ms-visio.drawing")
    if res["png"]:
        st.image(res["png"], caption="Preview (LibreOffice render - Visio may differ slightly)")


if __name__ == "__main__":
    main()
