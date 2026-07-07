#!/usr/bin/env python3
"""Fetch the FIFA Power Rankings (powered by Aramco) season feed and write a
compact powerranking.json for the site's Power tab and per-player power line.

Source (public, no auth) — FIFA Data Hub:
  https://fdh-api.fifa.com/v1/powerranking/season/285023.json

Each player carries 0-10 scores. Outfield: attacking / creativity / defending.
Goalkeepers: defending-the-goal / in-possession. `i` (playerId) is the FIFA
person id, i.e. the same id as squad players and fantasy `fid`, so scores map
straight onto our existing player data.

Usage: python3 scripts/update_powerranking.py [--dry-run]
"""
import os, sys, json, urllib.request

SEASON = "285023"
URL = f"https://fdh-api.fifa.com/v1/powerranking/season/{SEASON}.json"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "powerranking.json")


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        return json.loads(r.read())


def name(p):
    return next((x["description"] for x in p.get("playerName", []) if x.get("locale") == "en-GB"), "")


def team_code(p):
    # teamFlag = ".../picture/flags-{format}-{size}/POR" -> "POR"
    return (p.get("teamFlag") or "").rstrip("/").split("/")[-1]


def pic(p):
    return ((p.get("playerPicture") or {}).get("pictureUrl")) or ""


def r2(x):
    return round(float(x), 2) if x is not None else None


def build():
    d = get(URL)
    outfield = [{
        "i": p["playerId"], "p": name(p), "t": team_code(p), "pic": pic(p),
        "a": r2(p.get("attackingScore")), "c": r2(p.get("creativityScore")), "d": r2(p.get("defensiveScore")),
    } for p in d.get("outfieldPlayers", [])]
    gks = [{
        "i": p["playerId"], "p": name(p), "t": team_code(p), "pic": pic(p),
        "dg": r2(p.get("defendingTheGoalScore")), "ip": r2(p.get("inPossessionScore")),
    } for p in d.get("goalkeepers", [])]
    return {"n": d.get("nMatches"), "of": outfield, "gk": gks}


if __name__ == "__main__":
    data = build()
    print(f"{len(data['of'])} outfield + {len(data['gk'])} goalkeepers over {data['n']} matches")
    if "--dry-run" in sys.argv:
        print("(dry-run: not written)")
        sys.exit(0)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"Wrote {OUT} ({os.path.getsize(OUT)//1024} KB)")
