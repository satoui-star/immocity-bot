# ImmoCity Rental Watch Bot

Polls your ImmoCity search URL and pings you (Telegram and/or email) the moment
a **new** apartment is published. De-duplicates by the listing's property ID, so
you're notified exactly once per apartment.

Already tested against the live site — it correctly parses price, surface,
rooms, and location for each listing.

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
python immocity_bot.py --test
```

You should receive "✅ Test ImmoCity bot — les notifications fonctionnent !"

### (Optional) Email alerts instead of / in addition to Telegram
In `config.json` set `"email_enabled": true` and fill `email_user`,
`email_pass` (a Gmail **App Password**, not your real password — create one at
myaccount.google.com → Security → App passwords), and `email_to`.

## 3. Seed once (so you aren't spammed by existing listings)

```powershell
python immocity_bot.py --seed
```

This records every current listing **without** notifying. From now on you'll
only be alerted about genuinely new ones.

## 4. Run it

**Option A — leave it running in a terminal:**
```powershell
python immocity_bot.py --loop
```
Polls every `poll_minutes` (default 15). Ctrl+C to stop.

**Option B — Windows Task Scheduler (survives reboots, recommended):**
1. Open **Task Scheduler** → *Create Task*.
2. **Trigger:** *On a schedule* → *Repeat task every 15 minutes* → *indefinitely*.
3. **Action:** *Start a program*
   - Program/script: `python`
   - Arguments: `immocity_bot.py`
   - Start in: `C:\Users\SoumayaATAOUI\Downloads\immocity-bot`
4. Check *Run whether user is logged on or not*. Done.

Each run does a single polite page fetch, compares against `seen.json`, and
notifies only on new entries.

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
Edit `SEARCH_URL` inside `.github/workflows/watch.yml` (paste a new ImmoCity
search URL), commit, and push. No other changes needed.

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
| `immocity_bot.py` | the bot |
| `.github/workflows/watch.yml` | the cloud schedule (GitHub Actions) |
| `config.json` | LOCAL settings only — git-ignored, never pushed |
| `config.example.json` | template to copy from |
| `seen.json` | the bot's memory of seen listings (committed, shared with cloud) |
| `requirements.txt` | Python dependencies |

## Notes / etiquette
- Personal-use monitoring: one fetch per cycle, real browser User-Agent, 15-min
  default interval. Don't drop the interval below a few minutes.
- If ImmoCity changes its HTML, the parser may need a tweak — the `REF_RE` /
  `PRICE_RE` regexes at the top of the script are the place to look.
