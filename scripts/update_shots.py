#!/usr/bin/env python3
"""Extract per-player shot stats from FIFA's official player-stats feed into a
compact shots.json for the Top Cards "Shots on Target" board.

Source: https://fdh-api.fifa.com/v1/stats/season/<SEASON>/players.json
  (per-player list of ["StatName", value, bool]; ~5 MB, so we distil it here.)

Output: shots.json = { "<playerId>": [shots, onTarget, goals], ... } for every
player with at least one shot, keyed by FIFA person id (matches DATA.squads ids).
"""
import json, os, urllib.request

SEASON = "285023"  # FIFA World Cup 2026
URL = f"https://fdh-api.fifa.com/v1/stats/season/{SEASON}/players.json"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "shots.json")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def build():
    players = get(URL)
    out = {}
    for pid, stats in players.items():
        d = {s[0]: s[1] for s in stats if isinstance(s, list) and len(s) >= 2}
        shots = int(d.get("AttemptAtGoal", 0) or 0)
        if shots <= 0:
            continue
        on = int(d.get("AttemptAtGoalOnTarget", 0) or 0)
        goals = int(d.get("Goals", 0) or 0)
        out[str(pid)] = [shots, on, goals]
    return out


def main():
    data = build()
    with open(OUT, "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"wrote {OUT}: {len(data)} players with shots")


if __name__ == "__main__":
    main()
