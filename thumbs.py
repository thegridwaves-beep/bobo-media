#!/usr/bin/env python3
"""Thumbnail teardown: download, measure, and lay out competitor thumbnails.

Three modes.

  # 1. download the images (run on YOUR LAPTOP -- i.ytimg.com is blocked in the
  #    cloud session). Tries maxres -> sd -> hq until one exists.
  python3 thumbs.py fetch out/videos.csv -o thumbs/

  # 2. measure every image into a table (runs anywhere, no network)
  python3 thumbs.py scan thumbs/ -o out/

  # 3. build labelled contact sheets so a human -- or Claude -- can eyeball
  #    30 thumbnails at once instead of opening 30 files (runs anywhere)
  python3 thumbs.py sheet thumbs/ -o out/ --per-sheet 20

`scan` writes out/thumbs.csv: brightness, saturation, contrast, busyness,
flat-background %, warm-palette %, skin-tone %, dominant colours, and which
third of the frame carries the subject. Those are the numbers that separate a
thumbnail that gets clicked from one that gets scrolled past -- and unlike
"it looks punchy", you can sort by them.
"""
import argparse, colorsys, csv, glob, os, re, subprocess, sys, urllib.request
from collections import Counter

from PIL import Image, ImageDraw, ImageFilter, ImageFont

LADDER = ["maxresdefault", "sddefault", "hqdefault", "mqdefault"]
UA = {"User-Agent": "Mozilla/5.0"}

ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/live/|/embed/)([A-Za-z0-9_-]{11})")
BARE_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def vid_of(line):
    """Accept a bare id, any YouTube URL shape, or either with extra columns."""
    s = re.split(r"[\t, ]", line.strip(), maxsplit=1)[0]
    for a, b in (("%3D", "="), ("%3F", "?"), ("%2F", "/")):
        s = s.replace(a, b).replace(a.lower(), b)
    if BARE_RE.match(s):
        return s
    m = ID_RE.search(s)
    return m.group(1) if m else None

FONT_B = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"


def _font(size):
    try:
        return ImageFont.truetype(FONT_B, size)
    except OSError:
        return ImageFont.load_default()


# ---------------------------------------------------------------- fetch ----
def fetch(src, outdir):
    os.makedirs(outdir, exist_ok=True)
    ids, seen = [], set()
    if src.endswith(".csv"):
        lines = [r["video_id"] for r in csv.DictReader(open(src, encoding="utf-8"))]
    else:
        lines = list(open(src, encoding="utf-8"))
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        v = vid_of(line)
        if v and v not in seen:
            seen.add(v)
            ids.append(v)
    if not ids:
        raise SystemExit(f"no video ids found in {src}")

    # python.org macOS builds ship without a usable CA store until you run
    # /Applications/Python\ 3.x/Install\ Certificates.command, so prefer certifi
    ctx = None
    try:
        import certifi, ssl
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass

    def via_urllib(url):
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            return r.read()

    def via_curl(url):
        """Fallback: curl uses the system trust store and works where the
        python.org build's SSL does not."""
        out = subprocess.run(["curl", "-sSL", "--max-time", "25", "-A", UA["User-Agent"], url],
                             capture_output=True)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or b"").decode()[:160] or f"curl exit {out.returncode}")
        return out.stdout

    ok, first_err = 0, None
    for vid in ids:
        dest = os.path.join(outdir, f"{vid}.jpg")
        if os.path.exists(dest) and os.path.getsize(dest) > 2000:
            print(f"{vid}  already have it")
            ok += 1
            continue
        err = None
        for name in LADDER:
            url = f"https://i.ytimg.com/vi/{vid}/{name}.jpg"
            data = None
            for getter in (via_urllib, via_curl):
                try:
                    data = getter(url)
                    break
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
            if data is None:
                continue
            # YouTube serves a small grey placeholder rather than a 404
            if len(data) < 2000:
                err = f"placeholder only ({len(data)} bytes)"
                continue
            open(dest, "wb").write(data)
            print(f"{vid}  {name}  {len(data)//1024}kb")
            ok += 1
            break
        else:
            print(f"{vid}  NO THUMBNAIL  <- {err}")
            first_err = first_err or err
    print(f"\n{ok}/{len(ids)} downloaded to {outdir}")
    if ok == 0 and first_err:
        print(f"\nEvery attempt failed with: {first_err}")
        if "CERTIFICATE" in first_err.upper() or "SSL" in first_err.upper():
            print("That is the macOS python.org certificate problem. Fix it with:")
            print('  /Applications/Python\\ 3.14/Install\\ Certificates.command')
            print("  (or: pip3 install certifi, then re-run)")


# ----------------------------------------------------------------- scan ----
def hexof(rgb):
    return "#%02X%02X%02X" % tuple(int(c) for c in rgb[:3])


def measure(path):
    import numpy as np
    im = Image.open(path).convert("RGB")
    w, h = im.size
    small = im.resize((320, int(320 * h / w)) if w >= h else (int(320 * w / h), 320))
    a = np.asarray(small).astype("float32") / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]

    mx, mn = a.max(-1), a.min(-1)
    val = mx
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    lum = 0.299 * r + 0.587 * g + 0.114 * b

    # busyness: mean edge energy
    edges = np.asarray(small.convert("L").filter(ImageFilter.FIND_EDGES)).astype("float32")
    busy = float(edges.mean()) / 255.0

    # flat background: share of pixels in the single biggest 32-step colour bin
    q = (a * 255 // 32).astype("uint8")
    keys = (q[..., 0].astype(int) << 10) | (q[..., 1].astype(int) << 5) | q[..., 2]
    cnt = Counter(keys.ravel())
    flat = cnt.most_common(1)[0][1] / keys.size

    # palette: top colours from an adaptive quantisation of the real image
    pal = im.convert("RGB").quantize(colors=6, method=Image.Quantize.FASTOCTREE)
    p = pal.getpalette()
    tot = sum(n for n, _ in pal.getcolors())
    top = sorted(pal.getcolors(), reverse=True)[:4]
    dom = " ".join(f"{hexof(p[i*3:i*3+3])}:{round(100*n/tot)}%" for n, i in top)

    # warm/high-sat share -- the "loud" palette this niche runs on
    hue = np.zeros_like(lum)
    d = mx - mn
    nz = d > 1e-6
    hue[nz] = np.select(
        [mx[nz] == r[nz], mx[nz] == g[nz]],
        [((g[nz] - b[nz]) / d[nz]) % 6, (b[nz] - r[nz]) / d[nz] + 2],
        default=(r[nz] - g[nz]) / d[nz] + 4,
    ) * 60
    loud = (sat > 0.55) & (val > 0.45)
    warm = float((loud & ((hue < 60) | (hue > 330))).mean())

    # crude skin-tone share: a face or a character head dominating the frame
    skin = float((( (hue >= 5) & (hue <= 45) ) & (sat > 0.18) & (sat < 0.72) & (val > 0.35)).mean())

    # where the subject sits: busiest vertical third
    H, W = edges.shape
    thirds = [edges[:, :W // 3].mean(), edges[:, W // 3:2 * W // 3].mean(), edges[:, 2 * W // 3:].mean()]
    side = ["LEFT", "CENTER", "RIGHT"][int(max(range(3), key=lambda i: thirds[i]))]
    # big text usually lives in a high-edge horizontal band
    bands = [edges[:H // 3].mean(), edges[H // 3:2 * H // 3].mean(), edges[2 * H // 3:].mean()]
    textband = ["TOP", "MIDDLE", "BOTTOM"][int(max(range(3), key=lambda i: bands[i]))]

    return {
        "file": os.path.basename(path),
        "video_id": os.path.basename(path).split(".")[0],
        "px": f"{w}x{h}",
        "brightness": round(float(lum.mean()), 3),
        "contrast": round(float(lum.std()), 3),
        "saturation": round(float(sat.mean()), 3),
        "busyness": round(busy, 4),
        "flat_bg_pct": round(100 * flat, 1),
        "warm_loud_pct": round(100 * warm, 1),
        "skin_pct": round(100 * skin, 1),
        "subject_side": side,
        "text_band": textband,
        "dominant_colors": dom,
    }


def scan(srcdir, outdir):
    os.makedirs(outdir, exist_ok=True)
    files = sorted(f for e in ("jpg", "jpeg", "png", "webp")
                   for f in glob.glob(os.path.join(srcdir, f"*.{e}")))
    if not files:
        sys.exit(f"no images in {srcdir}")
    rows = []
    for f in files:
        try:
            rows.append(measure(f))
            print(f"{rows[-1]['video_id']}  bright {rows[-1]['brightness']}  "
                  f"busy {rows[-1]['busyness']}  flat {rows[-1]['flat_bg_pct']}%  "
                  f"{rows[-1]['subject_side']}/{rows[-1]['text_band']}")
        except Exception as ex:
            print(f"skip {f}: {ex}")
    dest = os.path.join(outdir, "thumbs.csv")
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"\nwrote {dest}  ({len(rows)} images)")


# ---------------------------------------------------------------- sheet ----
def sheet(srcdir, outdir, per, cols, titles_csv=None):
    os.makedirs(outdir, exist_ok=True)
    files = sorted(f for e in ("jpg", "jpeg", "png", "webp")
                   for f in glob.glob(os.path.join(srcdir, f"*.{e}")))
    if not files:
        sys.exit(f"no images in {srcdir}")

    titles = {}
    if titles_csv and os.path.exists(titles_csv):
        for row in csv.DictReader(open(titles_csv, encoding="utf-8")):
            titles[row["video_id"]] = row.get("title", "")

    CW, CH, PAD, CAP = 400, 225, 14, 34
    f_idx, f_cap = _font(22), _font(15)
    made = []
    for si in range(0, len(files), per):
        block = files[si:si + per]
        rows = (len(block) + cols - 1) // cols
        W = cols * CW + (cols + 1) * PAD
        H = rows * (CH + CAP) + (rows + 1) * PAD
        sheetim = Image.new("RGB", (W, H), (245, 245, 245))
        d = ImageDraw.Draw(sheetim)
        for i, path in enumerate(block):
            vid = os.path.basename(path).split(".")[0]
            im = Image.open(path).convert("RGB")
            im = im.resize((CW, CH))
            cx = PAD + (i % cols) * (CW + PAD)
            cy = PAD + (i // cols) * (CH + CAP + PAD)
            sheetim.paste(im, (cx, cy))
            d.rectangle([cx, cy, cx + CW, cy + CH], outline=(200, 200, 200))
            n = str(si + i + 1)
            d.rectangle([cx, cy, cx + 16 + 12 * len(n), cy + 30], fill=(226, 59, 46))
            d.text((cx + 8, cy + 4), n, font=f_idx, fill=(255, 255, 255))
            cap = titles.get(vid, vid)
            if len(cap) > 52:
                cap = cap[:51] + "…"
            d.text((cx + 2, cy + CH + 8), cap, font=f_cap, fill=(40, 40, 40))
        dest = os.path.join(outdir, f"contact_sheet_{si // per + 1:02d}.jpg")
        sheetim.save(dest, quality=88)
        made.append(dest)
        print("wrote", dest, sheetim.size)
    print(f"\n{len(made)} sheet(s), {len(files)} thumbnails")


def main():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("mode", choices=["fetch", "scan", "sheet"])
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--per-sheet", type=int, default=20)
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--titles", default="out/videos.csv",
                    help="videos.csv, to caption the sheet with real titles")
    a = ap.parse_args()
    if a.mode == "fetch":
        fetch(a.src, a.out)
    elif a.mode == "scan":
        scan(a.src, a.out)
    else:
        sheet(a.src, a.out, a.per_sheet, a.cols, a.titles)


if __name__ == "__main__":
    main()
