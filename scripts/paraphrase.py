#!/usr/bin/env python3
"""Manual goal-narrative paraphrasing helper (no API key — the rewrites come from
a Claude Code session).

Goal descriptions (`g.desc`) are extracted verbatim from FIFA's live blog. To
avoid publishing that editorial prose word-for-word, each one is paraphrased
once and marked `dp:1`. Freshly-fetched narratives from update_results.py carry
`dp:0` (or no dp) — those are the ones still to rewrite.

Deterministic document-order walk gives every goal-with-desc a stable index, so
paraphrases can be supplied as a simple {index: text} map.

Usage:
  python3 scripts/paraphrase.py extract [--all]   # -> scratch/para_src.json
      default: only goals needing paraphrase (dp != 1). --all: every desc.
  python3 scripts/paraphrase.py apply para_out.json   # {index: "rewrite"} -> data.json
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data.json")


def walk_goals(obj, out):
    """Collect goal dicts (those carrying a string `desc`) in document order."""
    if isinstance(obj, dict):
        if isinstance(obj.get("desc"), str) and obj["desc"].strip():
            out.append(obj)
        for v in obj.values():
            walk_goals(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_goals(v, out)


def load():
    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    goals = []
    walk_goals(data, goals)
    return data, goals


def cmd_extract(args):
    want_all = "--all" in args
    _, goals = load()
    rows = []
    for i, g in enumerate(goals):
        if want_all or g.get("dp") != 1:
            rows.append({"i": i, "id": g.get("id"), "desc": g["desc"]})
    dst = os.path.join(ROOT, "scratch_para_src.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"{len(rows)} of {len(goals)} goals need paraphrasing -> {dst}")


def nums(s):
    # numeric facts that must survive a rewrite (minutes, scores). Split on '-'
    # so "90+4" and "2-1" become individual integers.
    return sorted(re.findall(r"\d+", s))


def cmd_apply(args):
    if not args:
        sys.exit("usage: apply <para_out.json>")
    with open(args[0], encoding="utf-8") as f:
        para = json.load(f)
    para = {int(k): v for k, v in para.items()}
    data, goals = load()
    changed, warned = 0, 0
    for i, txt in para.items():
        g = goals[i]
        orig = g["desc"]
        missing = sorted(set(nums(orig)) - set(nums(txt)))
        if missing:
            warned += 1
            print(f"  ! goal[{i}] id={g.get('id')} dropped numbers {missing} — SKIPPED")
            continue
        g["desc"] = txt.strip()
        g["dp"] = 1
        changed += 1
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"applied {changed} paraphrases ({warned} skipped) -> data.json")
    remaining = sum(1 for g in goals if g.get("dp") != 1)
    print(f"{remaining} goals still need paraphrasing")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"extract": cmd_extract, "apply": cmd_apply}.get(cmd, lambda a: sys.exit(__doc__))(sys.argv[2:])
