#!/usr/bin/env python3
"""
Multi-source rental watch bot — ImmoCity + Foncia + Citya.

Polls all three sites on a schedule, keeps only apartments that pass your
filters (budget, surface, and commute time to École Vétérinaire de Maisons-
Alfort on métro line 8), scores each one, and pushes a Telegram / email alert
the moment a NEW matching listing appears.

Usage:
    python bot.py            # one polling cycle (use with cron / GitHub Actions)
    python bot.py --loop     # run forever, polling every poll_minutes
    python bot.py --seed     # record current listings WITHOUT notifying
                             #   (run ONCE first so you aren't spammed)
    python bot.py --test     # send a test notification and exit

State (the set of already-seen listings) lives in seen.json so it can be
committed back to the repo by GitHub Actions between runs.
"""

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText

import requests

from scrapers import scrape_all

# Console UTF-8 safety on Windows (emoji in logs won't crash).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "seen.json")
CONFIG_PATH = os.path.join(HERE, "config.json")

# --------------------------------------------------------------------------- #
#  COMMUTE MODEL — minutes by métro to École Vétérinaire de Maisons-Alfort     #
#  (line 8 terminus-side). Rough door-to-door metro estimates by postal code.  #
# --------------------------------------------------------------------------- #
COMMUTE_MIN = {
    "94700": 4,    # Maisons-Alfort — on line 8, basically there
    "94160": 6,    # Saint-Mandé (if added)
    "94220": 6,    # Charenton-le-Pont — 1–2 stops on line 8
    "75012": 15,   # Paris 12e — direct on line 8 (Daumesnil/Reuilly)
    "75011": 19,   # Paris 11e — line 8 via Bastille/Ledru-Rollin
    "75013": 24,   # Paris 13e — needs one change
    "75010": 26,   # Paris 10e — line 8 far end
    "75014": 30,   # Paris 14e — needs a change, south side
}

# Indicative rental €/m²/month per zone, for the "good value" half of the score.
MEDIAN_EUR_M2 = {
    "94700": 21, "94220": 24, "94160": 27,
    "75010": 33, "75011": 33, "75012": 31, "75013": 30, "75014": 31,
}
DEFAULT_MEDIAN = 30

# --------------------------------------------------------------------------- #
#  CONFIG                                                                       #
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "sources": ["immocity", "foncia", "citya", "flatbay"],

    "immocity_url": (
        "https://www.immocity.com/index.php?contr=biens_liste&tri_lots=date"
        "&type_transaction=1&localisation=Maisons+Alfort+-+94700"
        "&hidden-localisation=Paris+-+75013%2CParis+-+75011%2CCharenton+Le+Pont+-+94220"
        "%2CParis+-+75012%2CParis+-+75010%2CParis+-+75014%2CMaisons+Alfort+-+94700"
        "&type_lot%5B%5D=appartement&surface=20&nb_piece=0&nb_chambre=0"
        "&budget_min=&budget_max=1000&page=0&vendus=0&submit_search_1="
    ),
    "foncia_zones": [
        "maisons-alfort-94700", "charenton-le-pont-94220",
        "paris-75010", "paris-75011", "paris-75012", "paris-75013", "paris-75014",
    ],
    "citya_zones": [
        "maisons-alfort-94700", "charenton-le-pont-94220",
        "paris-10e-arrondissement-75010", "paris-11e-arrondissement-75011",
        "paris-12e-arrondissement-75012", "paris-13e-arrondissement-75013",
        "paris-14e-arrondissement-75014",
    ],
    # Flatbay (Altarea) filters by free-text city; we keep only target postals.
    "flatbay_zones": ["Maisons-Alfort", "Charenton-le-Pont", "Paris"],
    "flatbay_postals": ["94700", "94220", "75010", "75011", "75012", "75013", "75014"],

    # --- filters (same params as the ImmoCity search) ---
    "budget_max": 1000,        # €/month, inclusive
    "surface_min": 20,         # m²
    "max_commute_min": 40,     # hard cap on commute to École Vétérinaire
    "min_score": 0.0,          # raise (e.g. 6.0) to only get the best deals

    "poll_minutes": 15,

    # --- Telegram (recommended) ---
    "telegram_enabled": False,
    "telegram_token": "",
    "telegram_chat_id": "",

    # --- Email (optional) ---
    "email_enabled": False,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "email_user": "",
    "email_pass": "",
    "email_to": "",
}

ENV_OVERRIDES = {
    "telegram_enabled": "TELEGRAM_ENABLED",
    "telegram_token": "TELEGRAM_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "email_enabled": "EMAIL_ENABLED",
    "email_user": "EMAIL_USER",
    "email_pass": "EMAIL_PASS",
    "email_to": "EMAIL_TO",
    "immocity_url": "IMMOCITY_URL",
    "budget_max": "BUDGET_MAX",
    "min_score": "MIN_SCORE",
}


def _as_bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    elif not any(os.environ.get(v) for v in ENV_OVERRIDES.values()):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"[setup] Wrote template config to {CONFIG_PATH} — edit it, then re-run.")

    for key, env in ENV_OVERRIDES.items():
        if os.environ.get(env) is not None:
            val = os.environ[env]
            if key.endswith("_enabled"):
                cfg[key] = _as_bool(val)
            elif key in ("budget_max",):
                cfg[key] = int(val)
            elif key in ("min_score",):
                cfg[key] = float(val)
            else:
                cfg[key] = val
    return cfg


# --------------------------------------------------------------------------- #
#  STATE                                                                         #
# --------------------------------------------------------------------------- #
def key(listing):
    return f"{listing['source']}:{listing['ref']}"


def load_seen():
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
#  SCORING + FILTERING                                                          #
# --------------------------------------------------------------------------- #
def _clamp(x, lo=0.0, hi=10.0):
    return max(lo, min(hi, x))


def evaluate(listing):
    """Return (score, commute_min, eur_m2). score in 0–10, higher = better."""
    postal = listing.get("location")
    commute = COMMUTE_MIN.get(postal)  # None if unknown
    price, surface = listing.get("price"), listing.get("surface")

    if price and surface:
        eur_m2 = price / surface
        median = MEDIAN_EUR_M2.get(postal, DEFAULT_MEDIAN)
        value_score = _clamp(5 + (median - eur_m2) / median * 20)
    else:
        eur_m2 = None
        value_score = 5.0  # neutral when we can't compute

    commute_score = 10.0 if commute is None else _clamp(10 - commute / 4.0)
    score = round(0.5 * value_score + 0.5 * commute_score, 1)
    return score, commute, eur_m2


def passes_filters(cfg, listing, commute):
    # Require a parsed surface: real apartments always list one; parking boxes,
    # caves and garages (sometimes filed under "appartement") do not.
    surface = listing.get("surface")
    if not surface or surface < cfg["surface_min"]:
        return False
    if listing.get("price") and listing["price"] > cfg["budget_max"]:
        return False
    if commute is not None and commute > cfg["max_commute_min"]:
        return False
    return True


# --------------------------------------------------------------------------- #
#  NOTIFICATIONS                                                                #
# --------------------------------------------------------------------------- #
def fmt(listing, score, commute, eur_m2):
    bits = []
    if listing.get("price"):
        bits.append(f"{listing['price']}€/mois")
    if listing.get("surface"):
        bits.append(f"{listing['surface']}m²")
    if listing.get("rooms"):
        bits.append(listing["rooms"])
    line1 = "  •  ".join(bits) if bits else "Nouveau bien"

    loc = listing.get("location") or "?"
    commute_txt = f"~{commute}min" if commute is not None else "trajet ?"
    ppm = f"  •  {eur_m2:.0f}€/m²" if eur_m2 else ""
    src = listing["source"].capitalize()

    return (f"🏠 [{src}]  score {score}/10\n"
            f"{line1}\n"
            f"📍 {loc}  •  🚇 {commute_txt} → École Vétérinaire{ppm}\n"
            f"{listing['url']}")


def notify_telegram(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage"
    data = {"chat_id": cfg["telegram_chat_id"], "text": text}
    # Telegram throttles bots (~1 msg/sec to a chat). On 429 it tells us how
    # long to wait via retry_after — honour it and retry instead of dropping.
    for _ in range(4):
        r = requests.post(url, data=data, timeout=20)
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 2)
            time.sleep(wait + 1)
            continue
        r.raise_for_status()
        return
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


def push(cfg, text, subject="Nouvelle annonce (location)"):
    sent = False
    if cfg.get("telegram_enabled"):
        try:
            notify_telegram(cfg, text)
            sent = True
        except Exception as e:
            print(f"  [telegram] error: {e}")
    if cfg.get("email_enabled"):
        try:
            notify_email(cfg, subject, text)
            sent = True
        except Exception as e:
            print(f"  [email] error: {e}")
    if not sent:
        print("  [notify] (no channel) " + text.replace("\n", " | "))


# --------------------------------------------------------------------------- #
#  CORE CYCLE                                                                   #
# --------------------------------------------------------------------------- #
def cycle(cfg, seed=False):
    stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    print(f"[{stamp}] polling {', '.join(cfg['sources'])} …")
    seen = load_seen()
    listings = scrape_all(cfg)

    new_alerts = 0
    kept = 0
    for L in listings:
        score, commute, eur_m2 = evaluate(L)
        if not passes_filters(cfg, L, commute):
            continue
        if score < cfg["min_score"]:
            continue
        kept += 1
        k = key(L)
        if k in seen:
            continue
        seen[k] = {**L, "score": score, "commute": commute,
                   "first_seen": datetime.now().isoformat(timespec="seconds")}
        if not seed:
            push(cfg, fmt(L, score, commute, eur_m2))
            new_alerts += 1
            time.sleep(0.5)   # stay comfortably under Telegram's rate limit
    save_seen(seen)

    if seed:
        print(f"[{stamp}] seeded — {kept} matching listing(s) recorded silently.")
    elif new_alerts:
        print(f"[{stamp}] {new_alerts} NEW matching listing(s) — notified.")
    else:
        print(f"[{stamp}] no new matches ({kept} matching, all already seen).")


# --------------------------------------------------------------------------- #
#  ENTRYPOINT                                                                   #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Multi-source rental watch bot")
    ap.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--seed", action="store_true",
                    help="record current listings without notifying (run once first)")
    ap.add_argument("--test", action="store_true",
                    help="send a test notification and exit")
    args = ap.parse_args()

    cfg = load_config()

    if args.test:
        push(cfg, "✅ Test — le bot location (ImmoCity + Foncia + Citya) fonctionne !")
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
