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
from youtube_transcript_api.formatters import SRTFormatter
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

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


SRT = SRTFormatter()


def fetch_one(api, vid, languages, outdir):
    # v1.x is instance-based: the old YouTubeTranscriptApi.get_transcript
    # static method was removed, and snippets are objects, not dicts.
    fetched = api.fetch(vid, languages=languages)
    if not len(fetched):
        raise RuntimeError("empty transcript")
    path = os.path.join(outdir, f"{vid}.{fetched.language_code}.srt")
    open(path, "w", encoding="utf-8").write(SRT.format_transcript(fetched))
    last = fetched[-1]
    return len(fetched), last.start + last.duration, fetched.is_generated, fetched.language_code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="file of URLs/ids, or a single URL/id")
    ap.add_argument("-o", "--out", default="raw")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--sleep", type=float, default=1.5)
    ap.add_argument("--http-proxy", help="e.g. http://user:pass@host:port")
    ap.add_argument("--https-proxy")
    ap.add_argument("--webshare-user", help="Webshare rotating RESIDENTIAL proxy username")
    ap.add_argument("--webshare-pass")
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

    proxy = None
    if a.webshare_user and a.webshare_pass:
        proxy = WebshareProxyConfig(proxy_username=a.webshare_user,
                                    proxy_password=a.webshare_pass)
        print("routing through Webshare residential proxies")
    elif a.http_proxy or a.https_proxy:
        proxy = GenericProxyConfig(http_url=a.http_proxy,
                                   https_url=a.https_proxy or a.http_proxy)
        print("routing through the supplied proxy")
    api = YouTubeTranscriptApi(proxy_config=proxy) if proxy else YouTubeTranscriptApi()
    print(f"{len(ids)} videos -> {a.out}/\n")
    ok, failed, blocked = 0, [], False

    for i, vid in enumerate(ids, 1):
        if any(os.path.exists(os.path.join(a.out, f"{vid}.{c}.srt"))
               for c in (a.lang, "en", "en-US", "en-GB")):
            print(f"[{i}/{len(ids)}] {vid}  already have it, skipping")
            ok += 1
            continue
        try:
            n, dur, gen, code = fetch_one(api, vid, [a.lang], a.out)
            print(f"[{i}/{len(ids)}] {vid}  {n:>4} cues  "
                  f"{int(dur)//60}:{int(dur)%60:02d}  "
                  f"{'auto' if gen else 'manual'}/{code}")
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
        print("\nYouTube blocked this IP. Turn off any VPN and re-run, or fall back to\n"
              "transcript.py. If you ever need this on a server, pass --http-proxy or\n"
              "--webshare-user/--webshare-pass: cloud IPs are blocked by default.")
    if failed:
        print("failed:", " ".join(failed), "\nRe-run to retry only these.")


if __name__ == "__main__":
    main()
