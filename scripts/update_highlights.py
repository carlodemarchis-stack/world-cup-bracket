#!/usr/bin/env python3
"""Fill in match highlight links from the FIFA YouTube playlist.

Reads the playlist with yt-dlp (keyless), parses each title
("Highlights | TeamA X-Y TeamB | FIFA World Cup 2026™"), maps the two teams to
bracket codes and writes the video onto the matching match — group games get
the video id in `h`, knockout games get the full watch URL in `highlights`.
Only fills when the field is missing, so curated links are never overwritten.

Team names come straight from FIFA's API (same short names the channel uses),
so no hand-maintained alias table. Pure stdlib + yt-dlp.

Usage:
  python3 scripts/update_highlights.py            # write data.json if changed
  python3 scripts/update_highlights.py --dry-run  # report matches, write nothing
"""

import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.request

COMPETITION = "17"
SEASON = "285023"
LIST_ID = "PLBRLtDhTHh5o"
PLAYLIST = f"https://www.youtube.com/playlist?list={LIST_ID}"
MATCHES_API = ("https://api.fifa.com/api/v3/calendar/matches"
               f"?idCompetition={COMPETITION}&idSeason={SEASON}&count=500&language=en")
DASH = "–"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "data.json")

# "TeamA (2)1-1(3) TeamB" or "TeamA 2-1 TeamB" -> teamA, 90' score, teamB
SCORE_RE = re.compile(r"^(.+?)\s+(?:\(\d+\))?(\d+)-(\d+)(?:\(\d+\))?\s+(.+)$")


def norm(name):
    """Accent/case/punctuation-insensitive key for a team name."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(c for c in s.lower() if c.isalnum())


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def name_map():
    """{normalised team name -> bracket code} from the FIFA feed."""
    out = {}
    for m in fetch_json(MATCHES_API).get("Results", []):
        for side in ("Home", "Away"):
            t = m.get(side) or {}
            nm, code = t.get("TeamName"), t.get("Abbreviation")
            if isinstance(nm, list):
                nm = (nm[0] or {}).get("Description") if nm else None
            if nm and code:
                out[norm(nm)] = code
    return out


def playlist_videos():
    """[{id, title}] for the FIFA highlights playlist, via yt-dlp."""
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--no-warnings", "-J", PLAYLIST],
        capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"yt-dlp failed: {proc.stderr[-300:]}")
    data = json.loads(proc.stdout)
    return [{"id": e["id"], "title": e.get("title") or ""}
            for e in data.get("entries", []) if e.get("id")]


def parse_title(title):
    """('cotedivoire', 'norway', [1, 2]) from a highlights title, else None."""
    for seg in (s.strip() for s in title.split("|")):
        m = SCORE_RE.match(seg)
        if m:
            return norm(m.group(1)), norm(m.group(4)), sorted((int(m.group(2)), int(m.group(3))))
    return None


def build_targets(data):
    """Every playable match as a target: team pair, score multiset, and how to
    stamp the video onto it."""
    targets = []

    for letter, g in data.get("groups", {}).items():
        for mt in g.get("matches", []):
            s = mt.get("s")
            ss = sorted(int(x) for x in s.split(DASH)) if s and DASH in s else None
            targets.append({"pair": frozenset((mt["a"], mt["b"])), "score": ss,
                            "obj": mt, "field": "h"})

    teams = [t["code"] for t in data["teams"]]
    r32, sched = data["r32"], data["schedule"]

    def won(side, L, k):
        slot = r32[f"{side}:1:{k}"] if L == 1 else sched.get(f"{side}:{L}:{k}", {})
        return slot.get("winner")

    def add_ko(a, b, slot):
        if a and b and slot.get("score"):
            ss = sorted(int(x) for x in slot["score"].split(DASH))
            targets.append({"pair": frozenset((a, b)), "score": ss,
                            "obj": slot, "field": "highlights"})

    for side, off in (("R", 0), ("L", 16)):
        for k in range(8):
            add_ko(teams[off + 2 * k], teams[off + 2 * k + 1], r32[f"{side}:1:{k}"])
        for k in range(4):
            add_ko(won(side, 1, 2 * k), won(side, 1, 2 * k + 1), sched[f"{side}:2:{k}"])
        for k in range(2):
            add_ko(won(side, 2, 2 * k), won(side, 2, 2 * k + 1), sched[f"{side}:3:{k}"])
        add_ko(won(side, 3, 0), won(side, 3, 1), sched[f"{side}:4:0"])

    add_ko(won("R", 4, 0), won("L", 4, 0), data.get("final", {}))
    # third place: the two beaten semi-finalists
    def sf_loser(side):
        w = won(side, 4, 0)
        a, b = won(side, 3, 0), won(side, 3, 1)
        return (b if w == a else a) if (w and a and b) else None
    add_ko(sf_loser("R"), sf_loser("L"), data.get("thirdPlace", {}))

    return targets


def main():
    dry = "--dry-run" in sys.argv
    data = json.load(open(DATA_PATH, encoding="utf-8"))
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)

    codes = name_map()
    videos = playlist_videos()
    targets = build_targets(data)
    changes, unmatched = [], []

    for v in videos:
        if not v["title"].strip():
            continue  # unavailable/private video — no metadata to match on
        parsed = parse_title(v["title"])
        if not parsed:
            unmatched.append(f"unparsed: {v['title']}")
            continue
        na, nb, tscore = parsed
        ca, cb = codes.get(na), codes.get(nb)
        if not ca or not cb:
            unmatched.append(f"unknown team: {v['title']}")
            continue
        pair = frozenset((ca, cb))
        cands = [t for t in targets if t["pair"] == pair]
        if len(cands) > 1:  # same two teams twice (group + knockout) -> split by score
            cands = [t for t in cands if t["score"] == tscore] or cands
        if not cands:
            unmatched.append(f"no slot: {ca}-{cb} ({v['title']})")
            continue
        t = cands[0]
        if t["field"] == "h":
            if not t["obj"].get("h"):
                t["obj"]["h"] = v["id"]
                changes.append(f"group {ca}-{cb}: h={v['id']}")
        else:
            if not t["obj"].get("highlights"):
                t["obj"]["highlights"] = f"https://www.youtube.com/watch?v={v['id']}&list={LIST_ID}"
                changes.append(f"knockout {ca}-{cb}: highlights={v['id']}")

    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if unmatched:
        print(f"{len(unmatched)} video(s) not matched to an open slot:")
        for u in unmatched:
            print("  " + u)
    if after == before:
        print("No highlight changes.")
        return
    print(f"{len(changes)} change(s):")
    for c in changes:
        print("  " + c)
    if dry:
        print("(dry-run: not written)")
        return
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print("Wrote data.json")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
