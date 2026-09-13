#!/usr/bin/env python3
"""Fetch YouTube transcripts the same way every transcript website does it.

No API key, no yt-dlp, no ffmpeg, no pip install -- Python standard library only.

    python3 transcript.py data/links.txt -o raw/

Writes raw/<id>.en.srt (and .json with exact ms timings) per video.

HOW IT WORKS -- this is the documented method behind tactiq, downsub,
notegpt and the rest. None of them have special access:

  1. POST https://www.youtube.com/youtubei/v1/player with an InnerTube
     client context. InnerTube is the private API youtube.com's own player
     runs on. The ANDROID client is used because its caption URLs are not
     PoToken-gated the way the WEB client's are.
  2. Read .captions.playerCaptionsTracklistRenderer.captionTracks[] from the
     response -- each entry has a baseUrl for one caption track.
  3. GET that baseUrl with &fmt=json3 and parse the segments.

The only thing those websites have that this session does not is an IP
YouTube will answer. Run this from your laptop and it works.
"""
import argparse, html, json, os, re, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET

PLAYER = "https://www.youtube.com/youtubei/v1/player"

# ANDROID first: its timedtext URLs are not PoToken-gated. IOS and WEB are
# fallbacks for when one client is throttled.
CLIENTS = [
    {"clientName": "ANDROID", "clientVersion": "20.10.38", "androidSdkVersion": 30,
     "hl": "en", "gl": "US",
     "_ua": "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"},
    {"clientName": "IOS", "clientVersion": "20.10.4", "deviceModel": "iPhone16,2",
     "hl": "en", "gl": "US",
     "_ua": "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3 like Mac OS X)"},
    {"clientName": "WEB", "clientVersion": "2.20250301.00.00", "hl": "en", "gl": "US",
     "_ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/133.0 Safari/537.36"},
]

ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/live/|/embed/)([A-Za-z0-9_-]{11})")
BARE_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def vid_of(line):
    s = re.split(r"[\t, ]", line.strip(), maxsplit=1)[0]
    for a, b in (("%3D", "="), ("%3F", "?"), ("%2F", "/")):
        s = s.replace(a, b).replace(a.lower(), b)
    if BARE_RE.match(s):
        return s
    m = ID_RE.search(s)
    return m.group(1) if m else None


def _post(url, payload, ua, timeout=30):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": ua,
        "Accept-Language": "en-US,en",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _get(url, ua, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept-Language": "en-US,en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def caption_tracks(vid):
    """Return (tracks, ua) for the first InnerTube client that answers."""
    last = ""
    for c in CLIENTS:
        ua = c["_ua"]
        client = {k: v for k, v in c.items() if k != "_ua"}
        try:
            data = _post(PLAYER, {"context": {"client": client}, "videoId": vid}, ua)
        except Exception as e:
            last = f"{client['clientName']}: {e}"
            continue
        status = data.get("playabilityStatus", {}).get("status", "")
        if status not in ("OK", "LIVE_STREAM_OFFLINE", ""):
            last = f"{client['clientName']}: playabilityStatus={status}"
            continue
        tracks = (data.get("captions", {})
                      .get("playerCaptionsTracklistRenderer", {})
                      .get("captionTracks", []))
        if tracks:
            return tracks, ua
        last = f"{client['clientName']}: no caption tracks"
    raise RuntimeError(last or "no client answered")


def pick(tracks, lang="en"):
    """Prefer a manually uploaded track, fall back to the auto (asr) one."""
    exact = [t for t in tracks if t.get("languageCode", "").startswith(lang)]
    pool = exact or tracks
    manual = [t for t in pool if t.get("kind") != "asr"]
    return (manual or pool)[0]


def parse_json3(raw):
    out = []
    for ev in json.loads(raw).get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).strip()
        if not text or text == "\n":
            continue
        start = ev.get("tStartMs", 0) / 1000
        dur = ev.get("dDurationMs", 0) / 1000
        out.append((start, start + dur, re.sub(r"\s+", " ", html.unescape(text))))
    return out


def parse_xml(raw):
    out = []
    for el in ET.fromstring(raw).iter("text"):
        if not (el.text or "").strip():
            continue
        start = float(el.get("start", 0))
        dur = float(el.get("dur", 0))
        txt = html.unescape(re.sub(r"<[^>]+>", "", el.text))
        out.append((start, start + dur, re.sub(r"\s+", " ", txt).strip()))
    return out


def ts(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues):
    return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n"
                     for i, (a, b, t) in enumerate(cues, 1))


def fetch(vid, outdir, lang="en"):
    tracks, ua = caption_tracks(vid)
    tr = pick(tracks, lang)
    base = tr["baseUrl"]
    kind = tr.get("kind", "manual")
    code = tr.get("languageCode", "?")

    sep = "&" if "?" in base else "?"
    try:
        raw = _get(base + sep + "fmt=json3", ua)
        cues = parse_json3(raw)
    except Exception:
        cues = parse_xml(_get(base, ua))
    if not cues:
        raise RuntimeError("caption track returned no cues (PoToken gate?)")

    srt = os.path.join(outdir, f"{vid}.{code}.srt")
    open(srt, "w", encoding="utf-8").write(to_srt(cues))
    json.dump([{"start": round(a, 3), "end": round(b, 3), "text": t}
               for a, b, t in cues],
              open(os.path.join(outdir, f"{vid}.{code}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return len(cues), cues[-1][1], kind, code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="file of URLs/ids, or a single URL/id")
    ap.add_argument("-o", "--out", default="raw")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--sleep", type=float, default=1.5)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    if os.path.exists(a.src):
        lines = [l for l in open(a.src, encoding="utf-8")
                 if l.strip() and not l.lstrip().startswith("#")]
    else:
        lines = [a.src]
    ids, seen = [], set()
    for l in lines:
        v = vid_of(l)
        if v and v not in seen:
            seen.add(v); ids.append(v)
    if not ids:
        sys.exit(f"no video ids found in {a.src}")
    print(f"{len(ids)} videos -> {a.out}/\n")

    ok, failed = 0, []
    for i, vid in enumerate(ids, 1):
        if any(os.path.exists(os.path.join(a.out, f"{vid}.{c}.srt"))
               for c in (a.lang, "en", "en-US")):
            print(f"[{i}/{len(ids)}] {vid}  already have it, skipping")
            ok += 1
            continue
        try:
            n, dur, kind, code = fetch(vid, a.out, a.lang)
            print(f"[{i}/{len(ids)}] {vid}  {n:>4} cues  "
                  f"{int(dur)//60}:{int(dur)%60:02d}  {kind}/{code}")
            ok += 1
        except Exception as e:
            print(f"[{i}/{len(ids)}] {vid}  FAILED: {e}")
            failed.append(vid)
        time.sleep(a.sleep)

    print(f"\n{ok}/{len(ids)} transcripts in {a.out}/")
    if failed:
        print("failed:", " ".join(failed))
        print("Re-run to retry only these -- finished ones are skipped.")


if __name__ == "__main__":
    main()
