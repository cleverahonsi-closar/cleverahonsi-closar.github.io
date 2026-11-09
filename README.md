# Closar

One local server, two sides:

| Address | Who sees it | What it is |
|---|---|---|
| `http://127.0.0.1:8810/` | everyone (when deployed) | the public site: `site/index.html` |
| `http://127.0.0.1:8810/studio` | **only this computer** | the private studio: audit, leads, outreach, pipeline |

Start: double-click `start_closar.bat`.

## The studio
- **Audit**: paste websites or channels. It checks each one from outside for signs of a failing system (14 business areas + creator posting consistency), ranks the problems costliest first, and writes the message: main problem, then "I also noticed". Free: no Claude, no logins.
- **Check the signs**: tick what you see yourself (reviews, comments, Instagram, a test enquiry). The ranking and message update.
- **Leads / Today / Pipeline**: the CRM: new → messaged → replied → in talks → paid / lost, follow-ups after 3, 7 and 14 days.
- **Asking**: people posting that they need help (Reddit, Bluesky, Telegram, automation forums), scanned every hour while the studio runs.
- Nothing is ever sent for you: Copy / Open in Gmail, you press send, then "I sent it".
- Claude only runs on the buttons "Rewrite with Claude" and "Find businesses/creators".

## Files
- `areas.yaml`: the problem areas, signs, message lines and prices (also editable in Settings)
- `proof.yaml`: facts and links messages may use; `[ADD LINK]` until the portfolio is live
- `config.yaml`: sources, keywords, schedules
- `engine/`: lead engine adapted from Gig Radar (`scan`, `crm`, `prospects`…) + `audit.py`
- `data/closar.db`: your leads (not in git)

## Deploying the public site
Only `site/` goes online (GitHub Pages or similar). The studio is never deployed, and the server refuses `/studio` to any other computer.
