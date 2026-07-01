#!/usr/bin/env python3
"""Sync match results from FIFA's public API into data.json.

Results-only: scores, winners and penalty shootouts for group and knockout
matches, plus recomputed group-table stats. It never touches the curated
fields (teams, dates, times, venues, highlights, scorers) — those are left
exactly as they are, so a newly-played match keeps its scheduled date/venue
and just gains a score/winner.

The bracket's 48 team codes are identical to FIFA's `Abbreviation`, so every
match maps with no fuzzy name-matching:
  - group games  -> groups[<letter>].matches, by the two codes
  - knockout games -> the slot whose two participants are those codes
Knockout participants are derived bottom-up from winners, mirroring the app.

Pure stdlib. Usage:
  python3 scripts/update_results.py            # write data.json if changed
  python3 scripts/update_results.py --dry-run  # print changes, write nothing
Exit code 0 always on success; non-zero if the fetch/parse fails (so CI
never commits a half-written file).
"""

import json
import os
import sys
import urllib.request

COMPETITION = "17"
SEASON = "285023"  # FIFA World Cup 2026
API = ("https://api.fifa.com/api/v3/calendar/matches"
       f"?idCompetition={COMPETITION}&idSeason={SEASON}&count=500&language=en")
DASH = "–"  # en-dash, matching the existing data
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "data.json")


def desc(v):
    """FIFA localises many fields as [{Locale, Description}]; pull the text."""
    if isinstance(v, list):
        return (v[0] or {}).get("Description", "") if v else ""
    return v or ""


def fetch(url, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - retry on any transport/parse error
            last = e
    raise RuntimeError(f"FIFA fetch failed after {tries} tries: {last}")


def parse_fifa(matches):
    """Return (group_results, ko_results).

    group_results[letter] = list of {a,b,ha,hb} score records (a/b are codes)
    ko_results[frozenset({codeA,codeB})] = {
        scores:{code:int}, pens:{code:int}|None, winner:code, et:bool }
    """
    group = {}
    ko = {}
    for m in matches:
        home, away = m.get("Home"), m.get("Away")
        if not home or not away:
            continue
        hc, ac = home.get("Abbreviation"), away.get("Abbreviation")
        if not hc or not ac:
            continue
        hs, as_ = m.get("HomeTeamScore"), m.get("AwayTeamScore")
        if hs is None or as_ is None:
            continue  # not played yet
        stage = desc(m.get("StageName"))
        if "First Stage" in stage:
            letter = desc(m.get("GroupName")).replace("Group", "").strip()
            group.setdefault(letter, []).append(
                {"a": hc, "b": ac, "ha": int(hs), "hb": int(as_)})
        else:
            # Knockout: only accept a settled result (winner assigned).
            win_id = str(m.get("Winner") or "")
            id2code = {str(home.get("IdTeam")): hc, str(away.get("IdTeam")): ac}
            wcode = id2code.get(win_id)
            if not wcode:
                continue
            hp, ap = m.get("HomeTeamPenaltyScore"), m.get("AwayTeamPenaltyScore")
            pens = hp is not None and ap is not None
            ko[frozenset((hc, ac))] = {
                "scores": {hc: int(hs), ac: int(as_)},
                "pens": {hc: int(hp), ac: int(ap)} if pens else None,
                "winner": wcode,
                "et": m.get("ResultType") in (2, 3) or pens,
            }
    return group, ko


def apply_ko(slot, a, b, ko, changes, label):
    """Write a knockout result into `slot` (mutates it).

    Scores are stored winner-first (winner's goals first), which is the
    convention the team/match modals assume when splitting the string.
    """
    if not a or not b:
        return
    res = ko.get(frozenset((a, b)))
    if not res:
        return
    w = res["winner"]
    loser = a if b == w else b
    sc = res["scores"]
    if w not in sc or loser not in sc:
        return
    new = {"score": f"{sc[w]}{DASH}{sc[loser]}", "winner": w}
    if res["et"]:
        new["et"] = True
    if res["pens"]:
        pn = res["pens"]
        new["pens"] = True
        new["penScore"] = f"{pn[w]}{DASH}{pn[loser]}"
    for k, v in new.items():
        if slot.get(k) != v:
            changes.append(f"{label}: {k} {slot.get(k)!r} -> {v!r}")
            slot[k] = v
    # Clear stale AET/pens flags if FIFA no longer reports them.
    for k in ("et", "pens", "penScore"):
        if k not in new and k in slot:
            changes.append(f"{label}: drop {k}")
            del slot[k]


def winner_of(slot_dict, key):
    return (slot_dict.get(key) or {}).get("winner")


def main():
    dry = "--dry-run" in sys.argv
    data = json.load(open(DATA_PATH, encoding="utf-8"))
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)

    payload = fetch(API)
    matches = payload.get("Results") or []
    if not matches:
        raise RuntimeError("FIFA returned no matches")
    group_res, ko = parse_fifa(matches)
    changes = []

    # ---- Group stage: match scores + recomputed table stats (no reorder) ----
    for letter, g in data.get("groups", {}).items():
        results = group_res.get(letter, [])
        by_pair = {frozenset((r["a"], r["b"])): r for r in results}
        for mt in g.get("matches", []):
            r = by_pair.get(frozenset((mt["a"], mt["b"])))
            if not r:
                continue
            # Orient score to the stored a-b order.
            if r["a"] == mt["a"]:
                s = f"{r['ha']}{DASH}{r['hb']}"
            else:
                s = f"{r['hb']}{DASH}{r['ha']}"
            if mt.get("s") != s:
                changes.append(f"group {letter} {mt['a']}-{mt['b']}: {mt.get('s')!r} -> {s!r}")
                mt["s"] = s
        _recompute_table(letter, g, changes)

    # ---- Knockout: derive participants bottom-up, apply results ----------
    codes = [t["code"] for t in data["teams"]]
    r32 = data["r32"]
    sched = data["schedule"]

    for side, off in (("R", 0), ("L", 16)):
        for k in range(8):
            first, second = codes[off + 2 * k], codes[off + 2 * k + 1]
            apply_ko(r32[f"{side}:1:{k}"], first, second, ko, changes, f"{side}:1:{k}")
        for k in range(4):  # R16
            first = winner_of(r32, f"{side}:1:{2*k}")
            second = winner_of(r32, f"{side}:1:{2*k+1}")
            apply_ko(sched[f"{side}:2:{k}"], first, second, ko, changes, f"{side}:2:{k}")
        for k in range(2):  # QF
            first = winner_of(sched, f"{side}:2:{2*k}")
            second = winner_of(sched, f"{side}:2:{2*k+1}")
            apply_ko(sched[f"{side}:3:{k}"], first, second, ko, changes, f"{side}:3:{k}")
        # SF
        first = winner_of(sched, f"{side}:3:0")
        second = winner_of(sched, f"{side}:3:1")
        apply_ko(sched[f"{side}:4:0"], first, second, ko, changes, f"{side}:4:0")

    # Final: R semifinal winner vs L semifinal winner (app's a/b order).
    fin_a, fin_b = winner_of(sched, "R:4:0"), winner_of(sched, "L:4:0")
    apply_ko(data["final"], fin_a, fin_b, ko, changes, "final")
    # Third place: the two beaten semi-finalists.
    third_a = _loser(sched, "R:4:0")
    third_b = _loser(sched, "L:4:0")
    apply_ko(data["thirdPlace"], third_a, third_b, ko, changes, "third")

    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if after == before:
        print("No changes.")
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


def _loser(sched, key):
    slot = sched.get(key, {})
    w = slot.get("winner")
    if not w:
        return None
    # Participants of a SF slot = the two QF winners feeding it.
    side = key[0]
    a = winner_of(sched, f"{side}:3:0")
    b = winner_of(sched, f"{side}:3:1")
    if a and b:
        return b if w == a else a
    return None


def _recompute_table(letter, g, changes):
    """Recompute w/d/l/gf/ga from match scores; keep finishing order as-is."""
    stat = {row["code"]: {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0}
            for row in g.get("table", [])}
    for mt in g.get("matches", []):
        s = mt.get("s")
        if not s or DASH not in s:
            continue
        try:
            ga, gb = (int(x) for x in s.split(DASH))
        except ValueError:
            continue
        a, b = mt["a"], mt["b"]
        if a not in stat or b not in stat:
            continue
        stat[a]["gf"] += ga; stat[a]["ga"] += gb
        stat[b]["gf"] += gb; stat[b]["ga"] += ga
        if ga > gb:
            stat[a]["w"] += 1; stat[b]["l"] += 1
        elif gb > ga:
            stat[b]["w"] += 1; stat[a]["l"] += 1
        else:
            stat[a]["d"] += 1; stat[b]["d"] += 1
    for row in g.get("table", []):
        st = stat[row["code"]]
        for k in ("w", "d", "l", "gf", "ga"):
            if row.get(k) != st[k]:
                changes.append(f"group {letter} table {row['code']}.{k}: {row.get(k)} -> {st[k]}")
                row[k] = st[k]


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
