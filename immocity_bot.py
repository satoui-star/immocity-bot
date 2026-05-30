#!/usr/bin/env python3
"""
ImmoCity rental watch bot.

Polls an ImmoCity search-results URL on a schedule, detects newly published
apartments, and pushes a notification (Telegram and/or email) the moment a new
listing appears.

Designed for PERSONAL use: polite request rate, a single page fetch per run,
a real browser User-Agent, and local de-duplication so you are only ever
notified once per apartment.

Usage:
    python immocity_bot.py            # one polling cycle (use with cron/Task Scheduler)
    python immocity_bot.py --loop     # run forever, polling every POLL_MINUTES
    python immocity_bot.py --test     # send a test notification and exit
    python immocity_bot.py --seed     # record current listings WITHOUT notifying
                                      # (run this ONCE first so you don't get
                                      #  spammed by every existing listing)
"""

import argparse
import json
import os
import re
import sys
import time
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# Make console output UTF-8 safe on Windows (emoji in log lines won't crash).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --------------------------------------------------------------------------- #
#  CONFIG  — edit config.json (preferred) or the defaults below                #
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "seen.json")   # git-friendly memory of seen listings
CONFIG_PATH = os.path.join(HERE, "config.json")

# Map config keys -> environment variable names (used on GitHub Actions, where
# secrets are injected as env vars instead of being committed to config.json).
ENV_OVERRIDES = {
    "search_url": "SEARCH_URL",
    "telegram_enabled": "TELEGRAM_ENABLED",
    "telegram_token": "TELEGRAM_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "email_enabled": "EMAIL_ENABLED",
    "email_user": "EMAIL_USER",
    "email_pass": "EMAIL_PASS",
    "email_to": "EMAIL_TO",
}

DEFAULT_CONFIG = {
    # The exact search URL from your browser (paste yours here).
    "search_url": (
        "https://www.immocity.com/index.php?contr=biens_liste&tri_lots=date"
        "&type_transaction=1&localisation=Maisons+Alfort+-+94700"
        "&hidden-localisation=Paris+-+75013%2CParis+-+75011%2CCharenton+Le+Pont+-+94220"
        "%2CParis+-+75012%2CParis+-+75010%2CParis+-+75014%2CMaisons+Alfort+-+94700"
        "&type_lot%5B%5D=appartement&surface=20&nb_piece=0&nb_chambre=0"
        "&budget_min=&budget_max=1000&page=0&vendus=0&submit_search_1="
    ),
    "base_url": "https://www.immocity.com",
    "poll_minutes": 15,

    # --- Telegram (recommended: instant + free) ---
    "telegram_enabled": False,
    "telegram_token": "",          # from @BotFather
    "telegram_chat_id": "",        # your numeric chat id

    # --- Email (optional fallback / alternative) ---
    "email_enabled": False,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "email_user": "",              # sender Gmail
    "email_pass": "",              # Gmail APP password (not your real password)
    "email_to": "",                # where to receive alerts
}


def _as_bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    elif not any(os.environ.get(v) for v in ENV_OVERRIDES.values()):
        # No config and no env vars (i.e. a fresh local checkout): write a
        # template so the user has something to edit. On CI we skip this.
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"[setup] Wrote template config to {CONFIG_PATH} — edit it, then re-run.")

    # Environment variables (GitHub Secrets) override the file.
    for key, env in ENV_OVERRIDES.items():
        if os.environ.get(env) is not None:
            val = os.environ[env]
            cfg[key] = _as_bool(val) if key.endswith("_enabled") else val
    return cfg


# --------------------------------------------------------------------------- #
#  STORAGE  — a plain JSON file so it can be committed back to git on CI        #
# --------------------------------------------------------------------------- #
def load_seen():
    """Return {ref: {...listing...}} of everything we've already seen."""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_seen(seen):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
#  SCRAPING                                                                     #
# --------------------------------------------------------------------------- #
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# ImmoCity detail URLs look like:
#   /gera4518-location-appartement-2-pieces-paris-75013/appartement-13732.html
# The trailing number (13732) is the stable property ID we dedupe on.
REF_RE = re.compile(r"appartement-(\d+)\.html", re.IGNORECASE)
PRICE_RE = re.compile(r"([\d\s ]{2,})\s*€")
SURFACE_RE = re.compile(r"(\d{1,4})\s*m[²2]", re.IGNORECASE)
ROOMS_RE = re.compile(r"(\d+)\s*pi[eè]ce", re.IGNORECASE)
POSTAL_RE = re.compile(r"\b(\d{5})\b")


def _to_int(s):
    return int(re.sub(r"[^\d]", "", s)) if s and re.search(r"\d", s) else None


def fetch_listings(cfg):
    """Return a list of dicts: {ref, url, price, surface, rooms, location}."""
    resp = requests.get(cfg["search_url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = REF_RE.search(href)
        if not m:
            continue
        ref = m.group(1)
        if ref in found:
            continue  # same listing linked twice (image + title)

        url = urljoin(cfg["base_url"], href)
        # On ImmoCity the <a> tag itself holds the full card text, e.g.
        # "A LOUER 982€ CC /mois Appartement - 2 pièces - 32m² Paris 75013".
        # If a given <a> is just an image link with no text, fall back to the
        # nearest column container (col-sm-4) — but NEVER the whole grid.
        text = " ".join(a.get_text(" ", strip=True).split())
        if not PRICE_RE.search(text):
            col = a.find_parent("div", class_=re.compile(r"col-"))
            if col:
                text = " ".join(col.get_text(" ", strip=True).split())

        price_m = PRICE_RE.search(text)
        surf_m = SURFACE_RE.search(text)
        rooms_m = ROOMS_RE.search(text)
        postal_m = POSTAL_RE.search(href) or POSTAL_RE.search(text)

        found[ref] = {
            "ref": ref,
            "url": url,
            "price": _to_int(price_m.group(1)) if price_m else None,
            "surface": _to_int(surf_m.group(1)) if surf_m else None,
            "rooms": rooms_m.group(1) + " pièces" if rooms_m else None,
            "location": postal_m.group(1) if postal_m else None,
        }
    return list(found.values())


# --------------------------------------------------------------------------- #
#  NOTIFICATIONS                                                                #
# --------------------------------------------------------------------------- #
def fmt(listing):
    parts = []
    if listing.get("price"):
        parts.append(f"{listing['price']}€/mois")
    if listing.get("surface"):
        parts.append(f"{listing['surface']}m²")
    if listing.get("rooms"):
        parts.append(listing["rooms"])
    if listing.get("location"):
        parts.append(f"({listing['location']})")
    head = "  •  ".join(parts) if parts else "Nouveau bien"
    return f"🏠 {head}\n{listing['url']}"


def notify_telegram(cfg, text):
    r = requests.post(
        f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage",
        data={"chat_id": cfg["telegram_chat_id"], "text": text,
              "disable_web_page_preview": False},
        timeout=20,
    )
    r.raise_for_status()


def notify_email(cfg, subject, text):
    msg = MIMEText(text, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["email_user"]
    msg["To"] = cfg["email_to"]
    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
        s.starttls()
        s.login(cfg["email_user"], cfg["email_pass"])
        s.send_message(msg)


def push(cfg, text, subject="Nouvelle annonce ImmoCity"):
    sent = False
    if cfg.get("telegram_enabled"):
        try:
            notify_telegram(cfg, text)
            sent = True
        except Exception as e:
            print(f"[telegram] error: {e}")
    if cfg.get("email_enabled"):
        try:
            notify_email(cfg, subject, text)
            sent = True
        except Exception as e:
            print(f"[email] error: {e}")
    if not sent:
        print("[notify] (no channel enabled) " + text.replace("\n", " | "))


# --------------------------------------------------------------------------- #
#  CORE CYCLE                                                                   #
# --------------------------------------------------------------------------- #
def cycle(cfg, seed=False):
    seen = load_seen()
    try:
        listings = fetch_listings(cfg)
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] fetch error: {e}")
        return

    new = []
    for L in listings:
        if L["ref"] in seen:
            continue
        seen[L["ref"]] = {
            "url": L["url"], "price": L["price"], "surface": L["surface"],
            "rooms": L["rooms"], "location": L["location"],
            "first_seen": datetime.now().isoformat(timespec="seconds"),
        }
        if not seed:
            new.append(L)
    save_seen(seen)

    stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    if seed:
        print(f"[{stamp}] seeded {len(listings)} existing listing(s); "
              f"future runs will only notify on NEW ones.")
        return

    if new:
        print(f"[{stamp}] {len(new)} NEW listing(s)! notifying…")
        for L in new:
            push(cfg, fmt(L))
    else:
        print(f"[{stamp}] no new listings ({len(listings)} on page).")


# --------------------------------------------------------------------------- #
#  ENTRYPOINT                                                                   #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="ImmoCity rental watch bot")
    ap.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--seed", action="store_true",
                    help="record current listings without notifying (run once first)")
    ap.add_argument("--test", action="store_true",
                    help="send a test notification and exit")
    args = ap.parse_args()

    cfg = load_config()

    if args.test:
        push(cfg, "✅ Test ImmoCity bot — les notifications fonctionnent !")
        return

    if args.seed:
        cycle(cfg, seed=True)
        return

    if args.loop:
        interval = cfg["poll_minutes"] * 60
        print(f"[start] polling every {cfg['poll_minutes']} min. Ctrl+C to stop.")
        while True:
            cycle(cfg)
            time.sleep(interval)
    else:
        cycle(cfg)


if __name__ == "__main__":
    main()
