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
# Opt-in: also backfill assists onto already-recorded goals (one-off; the
# hourly cron runs without it, so it never re-fetches settled matches).
REFRESH_GOALS = "--refresh-goals" in sys.argv


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
        # Metadata available for every match (group or knockout) from the feed.
        id2code = {str(home.get("IdTeam")): hc, str(away.get("IdTeam")): ac}
        ref = next((desc(o.get("Name")) for o in (m.get("Officials") or [])
                    if o.get("OfficialType") == 1), None)
        att = m.get("Attendance")
        att = int(att) if att and str(att).isdigit() else None
        form = {c: t for c, t in ((hc, home.get("Tactics")), (ac, away.get("Tactics"))) if t}
        venue = desc((m.get("Stadium") or {}).get("Name")) or None
        meta = {
            "ids": (str(m.get("IdStage")), str(m.get("IdMatch"))),
            "id2code": id2code, "ref": ref or None, "att": att, "form": form or None,
            "venue": venue,
            "num": int(m["MatchNumber"]) if str(m.get("MatchNumber", "")).isdigit() else None,
        }
        stage = desc(m.get("StageName"))
        if "First Stage" in stage:
            letter = desc(m.get("GroupName")).replace("Group", "").strip()
            group.setdefault(letter, []).append(
                dict(a=hc, b=ac, ha=int(hs), hb=int(as_), **meta))
        else:
            # Knockout: only accept a settled result (winner assigned).
            wcode = id2code.get(str(m.get("Winner") or ""))
            if not wcode:
                continue
            hp, ap = m.get("HomeTeamPenaltyScore"), m.get("AwayTeamPenaltyScore")
            pens = hp is not None and ap is not None
            ko[frozenset((hc, ac))] = dict(
                scores={hc: int(hs), ac: int(as_)},
                pens={hc: int(hp), ac: int(ap)} if pens else None,
                winner=wcode,
                et=m.get("ResultType") in (2, 3) or pens,
                **meta,
            )
    return group, ko


def _surname(desc_txt):
    """The ALL-CAPS surname from a FIFA event description.

    'Harry KANE (England) scores!!' -> 'Kane'
    'Assisted by Erik LIRA.'         -> 'Lira'
    """
    part = desc_txt.split("(")[0].strip()
    words = [w.strip(".,!?;:'’") for w in part.split()]
    words = [w for w in words if w]
    caps = [w for w in words if w == w.upper() and any(c.isalpha() for c in w)]
    name = " ".join(caps) if caps else (words[-1] if words else part)
    return name.title()


def _minute_key(mm):
    m = mm.replace("'", "").strip()
    if "+" in m:
        base, extra = m.split("+", 1)
        return (int(base) if base.isdigit() else 999) + (int(extra) / 100 if extra.isdigit() else 0)
    return int(m) if m.isdigit() else 999


def fetch_timeline(ids, id2code):
    """Fetch a match timeline once; return (goals, var, cards).

    goals: [{t, p, m, a?, pen?}] (own goals credited to opponent, tagged '(o.g.)').
    var:   [{m, d, x?}] VAR review moments in minute order (blank decisions dropped).
    cards: [{m, p, t, c}] bookings ('Y'/'R') in minute order.
    Returns ([], [], []) on any failure — best-effort, never breaks the run.
    """
    stage, match = ids
    try:
        payload = fetch(f"https://api.fifa.com/api/v3/timelines/{COMPETITION}/{SEASON}/{stage}/{match}?language=en")
    except Exception:  # noqa: BLE001
        return [], [], []
    codes = list(id2code.values())
    events = payload.get("Event", []) or []

    # Assists come as their own events, sharing the goal's minute and team.
    assists = {}
    for e in events:
        if desc(e.get("TypeLocalized")).strip().lower() == "assist":
            tc = id2code.get(str(e.get("IdTeam")))
            nm = _surname(desc(e.get("EventDescription")))
            if tc and nm:
                assists.setdefault((e.get("MatchMinute") or "", tc), []).append(
                    (nm, str(e.get("IdPlayer") or "")))

    goals, used = [], {}
    for e in events:
        tl = desc(e.get("TypeLocalized")).strip().lower()
        ed = desc(e.get("EventDescription"))
        own = "own goal" in tl or "own goal" in ed.lower()
        # Count real goals: open play, in-play penalties, and own goals — but not
        # penalty-shootout attempts (Period 11, no match minute).
        if tl not in ("goal!", "penalty goal") and not own:
            continue
        mm = e.get("MatchMinute") or ""
        if e.get("Period") == 11 or not mm.strip():
            continue
        scorer_team = id2code.get(str(e.get("IdTeam")))
        if not scorer_team:
            continue
        team = scorer_team
        if own:  # an own goal counts for the other side
            other = [c for c in codes if c != scorer_team]
            if other:
                team = other[0]
        g = {"t": team, "p": _surname(ed) + (" (o.g.)" if own else ""),
             "m": mm, "_k": _minute_key(mm)}
        pid = str(e.get("IdPlayer") or "")
        if pid:
            g["id"] = pid
        if tl == "penalty goal":
            g["pen"] = True
        if not own:  # pair with an unused assist from the same team + minute
            lst = assists.get((mm, scorer_team), [])
            i = used.get((mm, scorer_team), 0)
            if i < len(lst):
                g["a"] = lst[i][0]
                if lst[i][1]:
                    g["aid"] = lst[i][1]
                used[(mm, scorer_team)] = i + 1
        goals.append(g)
    goals.sort(key=lambda g: g["_k"])
    for g in goals:
        g.pop("_k", None)

    var = []
    for i, e in enumerate(events):
        if e.get("Type") != 71:   # VAR review
            continue
        dtext = desc(e.get("EventDescription")).strip()
        mm = (e.get("MatchMinute") or "").strip()
        if not (dtext and mm):
            continue
        vk = _minute_key(mm)
        # Safe context only: the nearest same-incident event within ~4 minutes
        # (the player sent off / who scored the awarded goal or converted penalty).
        def near(pred):
            best, bd = None, 99
            for j in range(max(0, i - 3), min(len(events), i + 7)):
                ee = events[j]
                if j == i or not pred(ee):
                    continue
                d = abs(_minute_key(ee.get("MatchMinute") or "") - vk)
                if d <= 4 and d < bd:
                    bd, best = d, ee
            return best
        low, ev = dtext.lower(), None
        if "penalty given" in low:
            ev = near(lambda x: desc(x.get("TypeLocalized")).strip().lower() == "penalty goal")
        elif "red card" in low:
            ev = near(lambda x: "red card" in desc(x.get("TypeLocalized")).strip().lower())
        elif "goal awarded" in low:
            ev = near(lambda x: desc(x.get("TypeLocalized")).strip().lower() == "goal!")
        entry = {"m": mm, "d": dtext, "_k": vk}
        if ev:
            nm = _surname(desc(ev.get("EventDescription")))
            if nm:
                entry["x"] = nm
        var.append(entry)
    var.sort(key=lambda v: v["_k"])
    for v in var:
        v.pop("_k", None)

    cards = []
    for e in events:
        tl = desc(e.get("TypeLocalized")).strip().lower()
        if tl not in ("yellow card", "red card"):
            continue
        p = _surname(desc(e.get("EventDescription")))
        mm = (e.get("MatchMinute") or "").strip()
        if p and mm:
            entry = {"m": mm, "p": p, "t": id2code.get(str(e.get("IdTeam"))),
                     "c": "R" if "red" in tl else "Y", "_k": _minute_key(mm)}
            pid = str(e.get("IdPlayer") or "")
            if pid:
                entry["id"] = pid
            cards.append(entry)
    cards.sort(key=lambda c: c["_k"])
    for c in cards:
        c.pop("_k", None)
    return goals, var, cards


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
    enrich(slot, res, changes, label)


def enrich(slot, res, changes, label):
    """Fill referee, attendance, formations and scorers/assists onto a match
    slot (group or knockout) — only when missing, so curated data is kept."""
    for key in ("ref", "att", "form", "num"):
        if res.get(key) and not slot.get(key):
            slot[key] = res[key]
            changes.append(f"{label}: {key}={res[key]}")
    # Venue: always use FIFA's tournament stadium name, for consistency across
    # group and knockout matches (overwrites the earlier curated venues).
    if res.get("venue") and slot.get("venue") != res["venue"]:
        slot["venue"] = res["venue"]
        changes.append(f"{label}: venue={res['venue']}")
    if not res.get("ids"):
        return
    if not slot.get("goals"):
        g, varev, cardsev = fetch_timeline(res["ids"], res["id2code"])
        if g:
            slot["goals"] = g
            changes.append(f"{label}: +{len(g)} scorer(s)")
        if varev and not slot.get("var"):
            slot["var"] = varev
            changes.append(f"{label}: +{len(varev)} VAR")
        if cardsev and not slot.get("cards"):
            slot["cards"] = cardsev
            changes.append(f"{label}: +{len(cardsev)} card(s)")
    elif REFRESH_GOALS:
        # Re-fetch and merge: backfill assists, add any goals we were missing
        # (e.g. penalties FIFA types separately), plus VAR + cards — keeping
        # curated scorer names.
        fresh, varev, cardsev = fetch_timeline(res["ids"], res["id2code"])
        if varev and slot.get("var") != varev:   # refresh may add adjacent-event context
            slot["var"] = varev
            changes.append(f"{label}: VAR x{len(varev)}")
        if cardsev and slot.get("cards") != cardsev:
            slot["cards"] = cardsev
            changes.append(f"{label}: cards x{len(cardsev)}")
        remaining = list(slot["goals"])
        for fg in fresh:
            fk = _minute_key(fg["m"])   # normalise "90+5'" vs "90'+5'" when matching
            match = next((ex for ex in remaining
                          if _minute_key(ex.get("m", "")) == fk and ex.get("t") == fg["t"]), None)
            if match is not None:
                remaining.remove(match)
                if "a" not in match and fg.get("a"):
                    match["a"] = fg["a"]
                    changes.append(f"{label}: assist {match['p']} <- {fg['a']}")
                if fg.get("pen") and not match.get("pen"):
                    match["pen"] = True
                    changes.append(f"{label}: {match['p']} (pen)")
                if fg.get("id") and not match.get("id"):
                    match["id"] = fg["id"]        # backfill FIFA player id
                if fg.get("aid") and not match.get("aid"):
                    match["aid"] = fg["aid"]
            else:
                slot["goals"].append(fg)
                changes.append(f"{label}: +goal {fg['p']} {fg['m']}")
        slot["goals"].sort(key=lambda g: _minute_key(g.get("m", "")))


def winner_of(slot_dict, key):
    return (slot_dict.get(key) or {}).get("winner")


def assign_ko_venues(data, matches, changes):
    """Set every knockout slot's stadium from FIFA, mapped by bracket topology so
    even not-yet-played matches get the right (FIFA) venue: R32 by team pair,
    deeper rounds by matching the winner placeholders (e.g. W73/W75)."""
    def stadium(m):
        return desc((m.get("Stadium") or {}).get("Name")) or None
    by_pair, by_ph = {}, {}
    for m in matches:
        h, a = m.get("Home") or {}, m.get("Away") or {}
        if h.get("Abbreviation") and a.get("Abbreviation"):
            by_pair[frozenset((h["Abbreviation"], a["Abbreviation"]))] = m
        pa, pb = m.get("PlaceHolderA"), m.get("PlaceHolderB")
        if pa and pb:
            by_ph[frozenset((pa, pb))] = m

    teams = [t["code"] for t in data["teams"]]
    num, r32, sched = {}, data["r32"], data["schedule"]

    def setv(slot, m, label):
        v = stadium(m)
        if v and slot.get("venue") != v:
            slot["venue"] = v
            changes.append(f"{label}: venue={v}")

    def place(key, slot, m):
        if m:
            num[key] = str(m.get("MatchNumber"))
            setv(slot, m, key)

    for side, off in (("R", 0), ("L", 16)):
        for k in range(8):
            key = f"{side}:1:{k}"
            place(key, r32[key], by_pair.get(frozenset((teams[off + 2 * k], teams[off + 2 * k + 1]))))
    # deeper rounds: a match is found by the two feeding winners' placeholders
    def feeder(side, level, k):
        a, b = num.get(f"{side}:{level - 1}:{2 * k}"), num.get(f"{side}:{level - 1}:{2 * k + 1}")
        return by_ph.get(frozenset(("W" + a, "W" + b))) if a and b else None
    for side in ("R", "L"):
        for k in range(4):
            key = f"{side}:2:{k}"; place(key, sched[key], feeder(side, 2, k))
        for k in range(2):
            key = f"{side}:3:{k}"; place(key, sched[key], feeder(side, 3, k))
        place(f"{side}:4:0", sched[f"{side}:4:0"], feeder(side, 4, 0))
    ra, la = num.get("R:4:0"), num.get("L:4:0")
    if ra and la:
        setv(data["final"], by_ph.get(frozenset(("W" + ra, "W" + la))), "final")
    third = next((m for m in matches if "third" in desc(m.get("StageName")).lower()), None)
    if third:
        setv(data["thirdPlace"], third, "third")


def fetch_squads(matches, existing):
    """{code: [{n: jersey, p: name, pos: 0-3, id: FIFA player id}]} for every team,
    from the FIFA squad endpoint. Fills teams not present, or re-fetches a stored
    squad that predates player ids (fetch-once, self-healing)."""
    code2id = {}
    for m in matches:
        for side in ("Home", "Away"):
            t = m.get(side) or {}
            if t.get("Abbreviation") and t.get("IdTeam"):
                code2id[t["Abbreviation"]] = str(t["IdTeam"])
    out = dict(existing or {})
    for code, tid in sorted(code2id.items()):
        if code in out and "dob" in (out[code] or [{}])[0]:
            continue   # already have this squad with ids + photo + bio fields
        try:
            sq = fetch(f"https://api.fifa.com/api/v3/teams/{tid}/squad"
                       f"?idCompetition={COMPETITION}&idSeason={SEASON}&language=en")
        except Exception:  # noqa: BLE001 - best-effort; a missing team just isn't listed
            continue
        players = [{"n": int(p["JerseyNum"]), "p": desc(p.get("PlayerName")).title(),
                    "pos": p.get("Position"), "id": str(p.get("IdPlayer") or ""),
                    "pic": (p.get("PlayerPicture") or {}).get("PictureUrl") or "",
                    "dob": (p.get("BirthDate") or "")[:10],
                    "ht": int(p["Height"]) if p.get("Height") else None,
                    "wt": int(p["Weight"]) if p.get("Weight") else None}
                   for p in (sq.get("Players") or [])
                   if p.get("JerseyNum") and desc(p.get("PlayerName"))]
        if players:
            out[code] = sorted(players, key=lambda x: x["n"])
    return out


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
            enrich(mt, r, changes, f"group {letter} {mt['a']}-{mt['b']}")
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

    # Stadiums for every knockout slot (incl. not-yet-played), by bracket topology.
    assign_ko_venues(data, matches, changes)

    # Full squads (jersey numbers) — fetched once, then cached in data.json.
    squads = fetch_squads(matches, data.get("squads"))
    if squads and squads != data.get("squads"):
        changes.append(f"squads: {len(squads)} teams")
        data["squads"] = squads

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
