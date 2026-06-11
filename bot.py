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
    # Maisons-Alfort / Charenton — on line 8
    "94700": 4,    "94220": 6,
    # East suburbs — fast access via line 8 or Nation interchange
    "94160": 16,   # Saint-Mandé — line 1 → Nation → line 8
    "94300": 20,   # Vincennes — line 1 → Nation → line 8
    "94200": 24,   # Ivry-sur-Seine — walk/bus to Charenton → line 8
    "94250": 28,   # Gentilly — bus to Charenton → line 8
    "94270": 32,   # Le Kremlin-Bicêtre — line 7 → Opéra → line 8
    # Paris — close to line 8 (direct or 1 change)
    "75004": 22,   "75012": 15,   "75003": 24,   "75011": 19,
    "75001": 28,   "75020": 30,   "75002": 30,   "75013": 24,
    "75010": 26,   "75014": 30,   "75015": 28,   "75007": 30,
    "75005": 32,   "75009": 33,   "75006": 35,   "75008": 35,
    "75017": 38,   "75019": 38,   "75018": 40,   "75016": 45,
    # South/west suburbs
    "92120": 35,   # Montrouge — line 4 north then change
    "92130": 35,   # Issy-les-Moulineaux — T2 → Balard → line 8
    "92170": 35,   # Vanves — tram/bus → line 8 or line 13
    "92240": 36,   # Malakoff — line 13 then change
    "92100": 38,   # Boulogne-Billancourt — line 10 → La Motte-Picquet → line 8
    "92200": 40,   # Neuilly-sur-Seine — line 1 → Bastille → line 8
    "92110": 42,   # Clichy — line 13 then change
    "92300": 43,   # Levallois-Perret — line 3 → Opéra → line 8
    # East inner suburbs
    "93100": 28,   # Montreuil — line 9 → Nation → line 8
    "93170": 32,   # Bagnolet — line 3 → Nation → line 8
    "93260": 37,   # Les Lilas — line 11 → République → line 8
    "93400": 42,   # Saint-Ouen — line 13 then change
}

# Indicative rental €/m²/month per zone, for the "good value" half of the score.
MEDIAN_EUR_M2 = {
    "94700": 21, "94220": 24, "94160": 27, "94300": 26,
    "94200": 22, "94250": 22, "94270": 22,
    "75001": 48, "75002": 45, "75003": 40, "75004": 42, "75005": 38,
    "75006": 46, "75007": 42, "75008": 43, "75009": 38, "75010": 33,
    "75011": 33, "75012": 31, "75013": 30, "75014": 31, "75015": 32,
    "75016": 38, "75017": 34, "75018": 30, "75019": 28, "75020": 28,
    "92100": 30, "92110": 25, "92120": 26, "92130": 28, "92170": 26,
    "92200": 38, "92240": 24, "92300": 28,
    "93100": 22, "93170": 22, "93260": 23, "93400": 20,
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
        "&hidden-localisation="
        "Paris+-+75001%2CParis+-+75002%2CParis+-+75003%2CParis+-+75004"
        "%2CParis+-+75005%2CParis+-+75006%2CParis+-+75007%2CParis+-+75008"
        "%2CParis+-+75009%2CParis+-+75010%2CParis+-+75011%2CParis+-+75012"
        "%2CParis+-+75013%2CParis+-+75014%2CParis+-+75015%2CParis+-+75016"
        "%2CParis+-+75017%2CParis+-+75018%2CParis+-+75019%2CParis+-+75020"
        "%2CCharenton+Le+Pont+-+94220%2CMaisons+Alfort+-+94700"
        "%2CVincennes+-+94300%2CSaint+Mande+-+94160%2CIvry+sur+Seine+-+94200"
        "%2CGentilly+-+94250%2CLe+Kremlin+Bicetre+-+94270"
        "%2CBoulogne+Billancourt+-+92100%2CClichy+-+92110%2CMontrouge+-+92120"
        "%2CIssy+les+Moulineaux+-+92130%2CVanves+-+92170"
        "%2CNeuilly+sur+Seine+-+92200%2CMalakoff+-+92240%2CLevallois+Perret+-+92300"
        "%2CMontreuil+-+93100%2CBagnolet+-+93170%2CLes+Lilas+-+93260"
        "%2CSaint+Ouen+-+93400"
        "&type_lot%5B%5D=appartement&surface=20&nb_piece=0&nb_chambre=0"
        "&budget_min=&budget_max=1000&page=0&vendus=0&submit_search_1="
    ),
    "foncia_zones": [
        # Paris — all 20 arrondissements
        "paris-75001", "paris-75002", "paris-75003", "paris-75004",
        "paris-75005", "paris-75006", "paris-75007", "paris-75008",
        "paris-75009", "paris-75010", "paris-75011", "paris-75012",
        "paris-75013", "paris-75014", "paris-75015", "paris-75016",
        "paris-75017", "paris-75018", "paris-75019", "paris-75020",
        # Close suburbs (≤10 km from Paris ring)
        "maisons-alfort-94700", "charenton-le-pont-94220",
        "vincennes-94300", "saint-mande-94160",
        "ivry-sur-seine-94200", "gentilly-94250", "le-kremlin-bicetre-94270",
        "boulogne-billancourt-92100", "clichy-92110", "montrouge-92120",
        "issy-les-moulineaux-92130", "vanves-92170", "neuilly-sur-seine-92200",
        "malakoff-92240", "levallois-perret-92300",
        "montreuil-93100", "bagnolet-93170", "les-lilas-93260", "saint-ouen-93400",
    ],
    "citya_zones": [
        # Paris — all 20 arrondissements
        "paris-1er-arrondissement-75001", "paris-02e-arrondissement-75002",
        "paris-03e-arrondissement-75003", "paris-04e-arrondissement-75004",
        "paris-05e-arrondissement-75005", "paris-06e-arrondissement-75006",
        "paris-07e-arrondissement-75007", "paris-08e-arrondissement-75008",
        "paris-09e-arrondissement-75009", "paris-10e-arrondissement-75010",
        "paris-11e-arrondissement-75011", "paris-12e-arrondissement-75012",
        "paris-13e-arrondissement-75013", "paris-14e-arrondissement-75014",
        "paris-15e-arrondissement-75015", "paris-16e-arrondissement-75016",
        "paris-17e-arrondissement-75017", "paris-18e-arrondissement-75018",
        "paris-19e-arrondissement-75019", "paris-20e-arrondissement-75020",
        # Close suburbs
        "maisons-alfort-94700", "charenton-le-pont-94220",
        "vincennes-94300", "saint-mande-94160",
        "ivry-sur-seine-94200", "gentilly-94250", "le-kremlin-bicetre-94270",
        "boulogne-billancourt-92100", "clichy-92110", "montrouge-92120",
        "issy-les-moulineaux-92130", "vanves-92170", "neuilly-sur-seine-92200",
        "malakoff-92240", "levallois-perret-92300",
        "montreuil-93100", "bagnolet-93170", "les-lilas-93260", "saint-ouen-93400",
    ],
    # Flatbay (Altarea) filters by free-text city; we keep only target postals.
    "flatbay_zones": [
        "Maisons-Alfort", "Charenton-le-Pont", "Paris",
        "Vincennes", "Saint-Mandé", "Ivry-sur-Seine", "Gentilly",
        "Le Kremlin-Bicêtre", "Boulogne-Billancourt", "Clichy", "Montrouge",
        "Issy-les-Moulineaux", "Vanves", "Neuilly-sur-Seine", "Malakoff",
        "Levallois-Perret", "Montreuil", "Bagnolet", "Les Lilas", "Saint-Ouen",
    ],
    "flatbay_postals": [
        "94700", "94220", "94300", "94160", "94200", "94250", "94270",
        "75001", "75002", "75003", "75004", "75005", "75006", "75007",
        "75008", "75009", "75010", "75011", "75012", "75013", "75014",
        "75015", "75016", "75017", "75018", "75019", "75020",
        "92100", "92110", "92120", "92130", "92170", "92200", "92240", "92300",
        "93100", "93170", "93260", "93400",
    ],

    # --- filters (same params as the ImmoCity search) ---
    "budget_max": 1000,        # €/month, inclusive
    "surface_min": 20,         # m²
    "max_commute_min": 60,     # hard cap on commute to École Vétérinaire
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
