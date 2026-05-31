# Rental Watch Bot — ImmoCity + Foncia + Citya

Polls **three** rental sites (ImmoCity, Foncia, Citya) on a schedule and pings
you (Telegram and/or email) the moment a **new** apartment matching your
criteria is published. De-duplicates by each site's stable property ID, so you
are notified exactly once per apartment.

Every listing is **scored 0–10** on two things:
- **Value** — its €/m² versus the typical rent for that zone
- **Commute** — estimated métro time to **École Vétérinaire de Maisons-Alfort**
  (line 8)

Listings are filtered by **budget** (≤ 1000 €), **surface** (≥ 20 m²) and a hard
**commute cap** (≤ 40 min). Set `min_score` (e.g. `6.0`) to only hear about the
best deals.

> All three scrapers are tested against the live sites. Known traps are handled:
> Foncia decimal-cents prices (`953,72 €`) and gallery photo-count badges,
> Citya "annonces similaires" from other cities, and parking-boxes filed under
> "appartement" (filtered out because they have no surface).

---

## 1. Install (one time)

```powershell
cd C:\Users\SoumayaATAOUI\Downloads\immocity-bot
python -m pip install -r requirements.txt
```

## 2. Set up Telegram alerts (recommended — instant & free)

1. In Telegram, message **@BotFather** → `/newbot` → follow prompts → copy the **token**.
2. Message **@userinfobot** (or @RawDataBot) → it replies with your numeric **chat id**.
3. Open `config.json` (created automatically on first run) and fill in:

```json
{
  "telegram_enabled": true,
  "telegram_token": "123456:ABC-your-token",
  "telegram_chat_id": "987654321"
}
```

4. Test it:

```powershell
python bot.py --test
```

You should receive "✅ Test — le bot location (ImmoCity + Foncia + Citya) fonctionne !"

### (Optional) Email alerts instead of / in addition to Telegram
In `config.json` set `"email_enabled": true` and fill `email_user`,
`email_pass` (a Gmail **App Password**, not your real password — create one at
myaccount.google.com → Security → App passwords), and `email_to`.

## 3. Seed once (so you aren't spammed by existing listings)

```powershell
python bot.py --seed
```

This records every current matching listing **without** notifying. From now on
you'll only be alerted about genuinely new ones.

## 4. Run it

**Option A — leave it running in a terminal:**
```powershell
python bot.py --loop
```
Polls every `poll_minutes` (default 15). Ctrl+C to stop.

**Option B — Windows Task Scheduler (survives reboots):**
1. Open **Task Scheduler** → *Create Task*.
2. **Trigger:** *On a schedule* → *Repeat task every 15 minutes* → *indefinitely*.
3. **Action:** *Start a program*
   - Program/script: `python`
   - Arguments: `bot.py`
   - Start in: `C:\Users\SoumayaATAOUI\Downloads\immocity-bot`
4. Check *Run whether user is logged on or not*. Done.

> The recommended setup is **GitHub Actions** (next section) — no laptop needed.

Each run fetches each site's search pages, scores + filters the results,
compares against `seen.json`, and notifies only on new matches.

## Tuning the search

Everything lives in `config.json` (local) or `bot.py` `DEFAULT_CONFIG` (used by
the cloud run):
- `budget_max`, `surface_min`, `max_commute_min`, `min_score` — the filters
- `foncia_zones` / `citya_zones` — the town slugs to crawl (add/remove zones)
- `immocity_url` — paste a fresh ImmoCity search URL anytime
- `COMMUTE_MIN` (top of `bot.py`) — métro minutes per postal code, edit to taste

---

## Customising your search

Just paste a different ImmoCity search URL into `"search_url"` in `config.json`
(change towns, budget, surface, etc. on the website, then copy the address bar).
The parser works for any ImmoCity rental search.

---

# ☁️ Run it in the cloud with GitHub Actions (no laptop needed)

This is the recommended setup — it runs 24/7 on GitHub's servers for **free**,
survives you switching laptops, and needs nothing installed on your machine.

### How it stays "free" and reliable
- On a **public** repo, GitHub Actions minutes are **unlimited & free**. (The
  repo contains zero secrets — your Telegram token lives in encrypted Secrets,
  not in the code — so public is safe.)
- The bot's memory (`seen.json`) is **committed back to the repo** after each
  run, so the ephemeral cloud runners always remember what they've already sent.
  Because the bot keeps committing, GitHub never auto-disables the schedule.

### Steps

**1. Create the repo and push (run these in this folder):**
```powershell
git init
git add .
git commit -m "ImmoCity rental watch bot"
git branch -M main
```
Then create an **empty** repo on github.com (call it `immocity-bot`, set it
**Public**), and connect + push:
```powershell
git remote add origin https://github.com/<your-username>/immocity-bot.git
git push -u origin main
```

**2. Add your Telegram secrets** (GitHub repo → **Settings → Secrets and
variables → Actions → New repository secret**), create two:
| Name | Value |
|------|-------|
| `TELEGRAM_TOKEN` | the token from @BotFather |
| `TELEGRAM_CHAT_ID` | your numeric chat id from @userinfobot |

**3. Enable & test:** go to the **Actions** tab → enable workflows if prompted →
open **"ImmoCity watch"** → **Run workflow** to fire it manually once. Check the
log shows "no new listings" and that it ran clean.

That's it. From now on it runs **every 15 minutes** automatically and Telegrams
you the instant a new apartment appears.

### Changing your search later
Edit the zones / filters in `bot.py` `DEFAULT_CONFIG` (`foncia_zones`,
`citya_zones`, `immocity_url`, `budget_max`, `min_score`…), commit, and push.
You can also override `BUDGET_MAX` / `MIN_SCORE` via the `env:` block in
`.github/workflows/watch.yml` without touching the code.

### Notes
- Scheduled runs can be delayed a few minutes at GitHub's peak times — normal.
- Every run that finds something new makes a small `seen.json` commit. That's
  expected; it keeps the memory and keeps the schedule alive.
- Prefer a **private** repo? It also works, but free Actions minutes are capped
  (~2000/min per month). At every-15-min that's tight, so bump the cron in
  `watch.yml` to `*/30 * * * *` (every 30 min) if you go private.

---

## Files
| File | Purpose |
|------|---------|
| `bot.py` | the core: config, commute scoring, filtering, notifications, CLI |
| `scrapers.py` | the three site scrapers (ImmoCity, Foncia, Citya) |
| `.github/workflows/watch.yml` | the cloud schedule (GitHub Actions) |
| `config.json` | LOCAL settings only — git-ignored, never pushed |
| `config.example.json` | template to copy from |
| `seen.json` | the bot's memory of seen listings (committed, shared with cloud) |
| `requirements.txt` | Python dependencies |

## Notes / etiquette
- Personal-use monitoring: a handful of polite fetches per cycle, real browser
  User-Agent, 15-min default interval. Don't drop the interval below a few minutes.
- If a site changes its HTML, the parser may need a tweak — each scraper and its
  regexes live in `scrapers.py`, clearly separated per site.
