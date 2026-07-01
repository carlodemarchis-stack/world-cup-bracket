# World Cup Bracket — Road to the Final

A self-contained radial visualization of the World Cup 2026 knockout bracket:
32 teams around the rim, links converging on the trophy, with live schedule and
results driven by a JSON feed. Live controls let you switch node style
(flag/ring), theme (light/dark), accent color, and toggle names, results, and
the layout gaps.

**Live site:** https://carlodemarchis-stack.github.io/world-cup-bracket/

## Files

- `index.html` — the whole app (no build step, no dependencies).
- `data.json` — the schedule + results feed the page fetches on load.
- `server.py` — a tiny static server for local development only (not used by
  GitHub Pages, which serves the files over HTTPS directly).

## Updating scores and schedule

Everything the bracket shows comes from `data.json`. To record a result, edit
the one match under `r32` — change its `{date, time}` to a score plus winner:

```json
"R:1:1": { "score": "0–2", "winner": "NOR" }
```

The page derives the rest from `winner`: the winner's crest advances to the next
slot, the loser dims out, and the status dots + highlighting update. Add
`"et": true` / `"pens": true` for extra-time / penalty finishes. Later rounds
(`schedule`) accept the same `{score, winner}` shape once they're played.

Commit and push the change; GitHub Pages redeploys automatically. The page
fetches once on load, so a browser reload picks up the update.

## Local development

```bash
python3 server.py   # serves this folder at http://localhost:4173
```

Opening `index.html` directly via `file://` won't work — browsers block
`fetch()` of the local `data.json`, so it must be served over HTTP.
