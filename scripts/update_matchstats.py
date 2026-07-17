#!/usr/bin/env python3
"""Per-match team stats from FIFA's official stats hub into a compact matchstats.json.

The FIFA match centre pulls per-match stats from
  https://fdh-api.fifa.com/v1/stats/match/<IdIFES>/teams.json   (141 metrics/team)
keyed by the match's internal IdIFES (found in the calendar entry's Properties).

Output: matchstats.json = { "<matchNumber>": { "<CODE>": {poss,shots,onT,passes,passPct,corners,fouls,off}, ... } }
Only the readable subset used by the card "Stats" tab + possession pie is kept.
"""
import json, os, urllib.request

COMP, SEASON = "17", "285023"
CAL = f"https://api.fifa.com/api/v3/calendar/matches?idCompetition={COMP}&idSeason={SEASON}&count=400&language=en"
MATCH = "https://fdh-api.fifa.com/v1/stats/match/{}/teams.json"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "matchstats.json")


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
    return None


def build():
    cal = get(CAL).get("Results", [])
    out = {}
    for m in cal:
        num = m.get("MatchNumber")
        ifes = (m.get("Properties") or {}).get("IdIFES")
        if not num or not ifes or m.get("HomeTeamScore") is None:
            continue  # unnumbered or not played yet
        home, away = m.get("Home") or {}, m.get("Away") or {}
        id2code = {str(home.get("IdTeam")): home.get("IdCountry"),
                   str(away.get("IdTeam")): away.get("IdCountry")}
        try:
            tj = get(MATCH.format(ifes))
        except Exception:
            continue
        if not isinstance(tj, dict) or not tj:
            continue
        entry = {}
        for tid, stats in tj.items():
            code = id2code.get(str(tid))
            if not code:
                continue
            s = {x[0]: x[1] for x in stats if isinstance(x, list) and len(x) >= 2}
            passes = int(s.get("Passes", 0) or 0)
            comp = int(s.get("PassesCompleted", 0) or 0)
            entry[code] = {
                "poss": round((s.get("Possession", 0) or 0) * 100),
                "shots": int(s.get("AttemptAtGoal", 0) or 0),
                "onT": int(s.get("AttemptAtGoalOnTarget", 0) or 0),
                "passes": passes,
                "passPct": round(comp / passes * 100) if passes else 0,
                "corners": int(s.get("Corners", 0) or 0),
                "fouls": int(s.get("FoulsAgainst", 0) or 0),
                "off": int(s.get("Offsides", 0) or 0),
            }
        if len(entry) == 2:
            out[str(num)] = entry
    return out


def main():
    data = build()
    with open(OUT, "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"wrote {OUT}: {len(data)} matches with stats")


if __name__ == "__main__":
    main()
