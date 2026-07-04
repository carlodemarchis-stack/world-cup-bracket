#!/usr/bin/env python3
"""Fetch the FIFA World Cup 2026 Fantasy player pool and write a compact
fantasy.json for the site's Fantasy tab.

Source feeds (public, no auth) — see FIFA_FANTASY_GUIDE.md:
  https://play.fifa.com/json/fantasy/players.json   (~1489 rows)
  https://play.fifa.com/json/fantasy/squads.json    (48 teams)

We drop `transferred` rows (stale duplicates) — the guide's recommended
approximation of the real tournament roster (nominal 48x26 = 1248). Output is
pre-sorted per the official UI order: ownership % desc, then total points desc.

Usage: python3 scripts/update_fantasy.py [--dry-run]
"""
import os, sys, json, urllib.request

BASE = "https://play.fifa.com/json/fantasy/{}.json"
UA = {"User-Agent": "Mozilla/5.0"}
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fantasy.json")


def get(name):
    url = BASE.format(name)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        return json.loads(r.read())


def player_name(p):
    return p.get("knownName") or f"{p.get('firstName','')} {p.get('lastName','')}".strip()


def build():
    players = get("players")
    squads = {s["id"]: s for s in get("squads")}
    teams = {}
    for s in squads.values():
        teams[s["abbr"]] = {"name": s["name"], "elim": bool(s.get("isEliminated"))}
    rows = []
    for p in players:
        if p.get("status") == "transferred":       # stale duplicate — drop
            continue
        sq = squads.get(p.get("squadId"), {})
        st = (p.get("stats") or {})
        rows.append({
            "n": player_name(p),
            "t": sq.get("abbr", "?"),
            "pos": p.get("position"),               # GK | DEF | MID | FWD
            "o": round(float(p.get("percentSelected") or 0), 1),  # ownership %
            "pts": int(st.get("totalPoints") or 0),
            "avg": round(float(st.get("avgPoints") or 0), 1),
            "pr": p.get("price"),                   # in-game price (millions)
            "st": p.get("status"),                  # playing | eliminated | injured | suspended
            "fid": p.get("fifaId"),                 # -> headshot
        })
    # Official UI order: ownership desc, tie-broken by total points.
    rows.sort(key=lambda r: (r["o"], r["pts"]), reverse=True)
    from collections import Counter
    by_pos = Counter(r["pos"] for r in rows)
    return {"teams": teams, "players": rows}, by_pos


if __name__ == "__main__":
    data, by_pos = build()
    n = len(data["players"])
    print(f"{n} players (excl. transferred) — " + "  ".join(f"{k}:{v}" for k, v in sorted(by_pos.items())))
    if "--dry-run" in sys.argv:
        print("(dry-run: not written)")
        sys.exit(0)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"Wrote {OUT} ({os.path.getsize(OUT)//1024} KB)")
