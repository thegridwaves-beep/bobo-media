#!/usr/bin/env python3
"""Transcript fetcher built on youtube-transcript-api -- the fallback path.

    pip3 install youtube-transcript-api
    python3 transcript_lib.py data/links.txt -o raw/

Try transcript.py first: it needs no packages at all. Use this one when
InnerTube starts refusing, since the library carries its own retry and
consent-cookie handling.

Unlike the plain-text version, this keeps the timings and writes SRT, which
is what beats.py needs -- without start/duration there is no hook window,
no WPM and no pacing comparison.
"""
import argparse, os, re, sys, time

from youtube_transcript_api import (
    YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound,
    VideoUnavailable, IpBlocked, RequestBlocked, PoTokenRequired, InvalidVideoId,
)

# anchored on real URL markers -- a bare "/" alternative matches any 11-char
# path segment and silently pulls the wrong id out of wrapper links
ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/live/|/embed/)([A-Za-z0-9_-]{11})")
BARE_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(s: str) -> str | None:
    s = re.split(r"[\t, ]", s.strip(), maxsplit=1)[0]
    for a, b in (("%3D", "="), ("%3F", "?"), ("%2F", "/")):
        s = s.replace(a, b).replace(a.lower(), b)
    if BARE_RE.match(s):
        return s
    m = ID_RE.search(s)
    return m.group(1) if m else None


def ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fetch_one(api, vid, languages, outdir):
    # v1.x is instance-based: the old YouTubeTranscriptApi.get_transcript
    # static method was removed, and snippets are objects, not dicts.
    fetched = api.fetch(vid, languages=languages)
    rows = fetched.to_raw_data()          # [{'text','start','duration'}, ...]
    if not rows:
        raise RuntimeError("empty transcript")

    srt = []
    for i, r in enumerate(rows, 1):
        start = r["start"]
        end = start + r.get("duration", 0)
        text = re.sub(r"\s+", " ", r["text"]).strip()
        if not text:
            continue
        srt.append(f"{i}\n{ts(start)} --> {ts(end)}\n{text}\n")
    path = os.path.join(outdir, f"{vid}.en.srt")
    open(path, "w", encoding="utf-8").write("\n".join(srt))
    return len(rows), rows[-1]["start"] + rows[-1].get("duration", 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="file of URLs/ids, or a single URL/id")
    ap.add_argument("-o", "--out", default="raw")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--sleep", type=float, default=1.5)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    src = [l for l in open(a.src, encoding="utf-8")] if os.path.exists(a.src) else [a.src]
    ids, seen = [], set()
    for line in src:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        v = extract_video_id(line)
        if v and v not in seen:
            seen.add(v)
            ids.append(v)
    if not ids:
        sys.exit(f"no video ids found in {a.src}")

    api = YouTubeTranscriptApi()
    print(f"{len(ids)} videos -> {a.out}/\n")
    ok, failed, blocked = 0, [], False

    for i, vid in enumerate(ids, 1):
        dest = os.path.join(a.out, f"{vid}.en.srt")
        if os.path.exists(dest):
            print(f"[{i}/{len(ids)}] {vid}  already have it, skipping")
            ok += 1
            continue
        try:
            n, dur = fetch_one(api, vid, [a.lang], a.out)
            print(f"[{i}/{len(ids)}] {vid}  {n:>4} cues  {int(dur)//60}:{int(dur)%60:02d}")
            ok += 1
        except (IpBlocked, RequestBlocked):
            print(f"[{i}/{len(ids)}] {vid}  BLOCKED -- YouTube is refusing this IP")
            blocked = True
            failed.append(vid)
            break
        except PoTokenRequired:
            print(f"[{i}/{len(ids)}] {vid}  needs a PoToken -- use transcript.py instead")
            failed.append(vid)
        except TranscriptsDisabled:
            print(f"[{i}/{len(ids)}] {vid}  transcripts disabled by the uploader")
            failed.append(vid)
        except NoTranscriptFound:
            print(f"[{i}/{len(ids)}] {vid}  no '{a.lang}' track")
            failed.append(vid)
        except (VideoUnavailable, InvalidVideoId):
            print(f"[{i}/{len(ids)}] {vid}  unavailable or bad id")
            failed.append(vid)
        except Exception as e:
            print(f"[{i}/{len(ids)}] {vid}  FAILED: {type(e).__name__}: {e}")
            failed.append(vid)
        time.sleep(a.sleep)

    print(f"\n{ok}/{len(ids)} transcripts in {a.out}/")
    if blocked:
        print("\nYouTube blocked the IP. You are probably on a VPN or office network --\n"
              "turn the VPN off and re-run, or fall back to transcript.py.")
    if failed:
        print("failed:", " ".join(failed), "\nRe-run to retry only these.")


if __name__ == "__main__":
    main()
