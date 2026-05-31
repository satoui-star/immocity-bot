"""
Per-site scrapers for the rental watch bot.

Each scraper returns a list of *normalized* listing dicts:

    {
        "source":   "immocity" | "foncia" | "citya",
        "ref":      "<stable id unique within the source>",
        "url":      "<absolute link to the listing>",
        "price":    int | None,    # € / month
        "surface":  int | None,    # m²
        "rooms":    str | None,    # e.g. "2 pièces"
        "location": str | None,    # postal code, e.g. "94700"
    }

The global key used for de-duplication is f"{source}:{ref}".

All three French sites return server-rendered HTML (verified live), so plain
requests + BeautifulSoup is enough — no headless browser needed.
"""

import re
import time
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}
TIMEOUT = 30

# --- shared field extraction --------------------------------------------- #
# Thousands separators on FR sites: regular space, NBSP ( ), narrow NBSP
# ( ). Price is always the number immediately before the € sign.
_PRICE_RE = re.compile(r"(\d[\d\s  ]{0,9}?)(?:[.,]\d{1,2})?\s*€")
_SURFACE_RE = re.compile(r"(\d{1,4})(?:[.,]\d+)?\s*m[²2]", re.IGNORECASE)
_ROOMS_RE = re.compile(r"(\d+)\s*pi[eè]ces?", re.IGNORECASE)
_POSTAL_RE = re.compile(r"\b(\d{5})\b")


def _digits(s):
    s = re.sub(r"[^\d]", "", s or "")
    return int(s) if s else None


def extract_fields(text, href=""):
    """Pull price / surface / rooms / postal from a card's text (+ its href)."""
    text = " ".join((text or "").split())
    price_m = _PRICE_RE.search(text)
    surf_m = _SURFACE_RE.search(text)
    rooms_m = _ROOMS_RE.search(text)
    # Postal code is most reliable from the URL; fall back to the text.
    postal_m = _POSTAL_RE.search(href) or _POSTAL_RE.search(text)
    return {
        "price": _digits(price_m.group(1)) if price_m else None,
        "surface": int(surf_m.group(1)) if surf_m else None,
        "rooms": (rooms_m.group(1) + " pièces") if rooms_m else None,
        "location": postal_m.group(1) if postal_m else None,
    }


def _get(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


# ========================================================================= #
#  IMMOCITY  — one search URL already covers every zone                      #
# ========================================================================= #
_IMMO_REF_RE = re.compile(r"appartement-(\d+)\.html", re.IGNORECASE)


def scrape_immocity(cfg):
    out, seen_ref = [], set()
    soup = BeautifulSoup(_get(cfg["immocity_url"]), "html.parser")
    for a in soup.find_all("a", href=True):
        m = _IMMO_REF_RE.search(a["href"])
        if not m:
            continue
        ref = m.group(1)
        if ref in seen_ref:
            continue
        seen_ref.add(ref)
        text = " ".join(a.get_text(" ", strip=True).split())
        if not _PRICE_RE.search(text):
            col = a.find_parent("div", class_=re.compile(r"col-"))
            if col:
                text = col.get_text(" ", strip=True)
        f = extract_fields(text, a["href"])
        out.append({"source": "immocity", "ref": ref,
                    "url": urljoin("https://www.immocity.com", a["href"]), **f})
    return out


# ========================================================================= #
#  FONCIA  — uses the Angular app's JSON backend API.                         #
#  The public website (fr.foncia.com) WAF-blocks datacenter IPs with a 403,  #
#  but the data API on a separate host returns clean structured JSON and is   #
#  reachable from the cloud. We query it directly, one POST per zone.         #
# ========================================================================= #
_FONCIA_API = "https://fnc-api.prod.fonciatech.net/annonces/annonces/search"
_FONCIA_API_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Origin": "https://fr.foncia.com",
    "Referer": "https://fr.foncia.com/",
}
_FONCIA_POSTAL_RE = re.compile(r"-(\d{5})/")


def _foncia_post(slug):
    """POST the search API for one locality slug, with a small retry."""
    payload = {
        "type": "location",
        "filters": {"localities": {"slugs": [slug]}},
        "expandNearby": False,   # stay strictly in the requested zone
        "size": 50,
    }
    last = None
    for attempt in range(3):
        try:
            r = requests.post(_FONCIA_API, headers=_FONCIA_API_HEADERS,
                              json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:                       # transient network / 5xx
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def scrape_foncia(cfg):
    out, seen_ref = [], set()
    for slug in cfg["foncia_zones"]:
        try:
            data = _foncia_post(slug)
        except Exception as e:
            print(f"  [foncia:{slug}] api error: {e}")
            continue
        for a in data.get("annonces", []):
            if "appartement" not in (a.get("typeBien") or "").lower():
                continue
            ref = str(a.get("reference") or "")
            if not ref or ref in seen_ref:
                continue
            seen_ref.add(ref)
            canon = a.get("canonicalUrl") or ""
            surf = a.get("surface") or {}
            loyer = a.get("loyer")
            pm = _FONCIA_POSTAL_RE.search(canon)
            out.append({
                "source": "foncia",
                "ref": ref,
                "url": urljoin("https://fr.foncia.com", canon),
                "price": int(round(loyer)) if isinstance(loyer, (int, float)) else None,
                "surface": int(round(surf["habitable"])) if surf.get("habitable") else None,
                "rooms": (f"{a['nbPiece']} pièces" if a.get("nbPiece") else None),
                "location": pm.group(1) if pm else None,
            })
    return out


# ========================================================================= #
#  CITYA  — one page per zone, price lives on the .property-card container    #
# ========================================================================= #
_CITYA_REF_RE = re.compile(r"/(GES\d+-\d+)")


def scrape_citya(cfg):
    out, seen_ref = [], set()
    for slug in cfg["citya_zones"]:
        url = (f"https://www.citya.com/annonces/location/appartement/{slug}"
               "?sort=b.dateCreation&direction=desc")
        try:
            soup = BeautifulSoup(_get(url), "html.parser")
        except Exception as e:
            print(f"  [citya:{slug}] fetch error: {e}")
            continue
        for card in soup.select("div.property-card"):
            a = card.find("a", href=_CITYA_REF_RE)
            if not a:
                continue
            href = a["href"]
            # Citya pads sparse zone pages with "annonces similaires" from other
            # cities. Keep only cards that actually belong to the queried zone.
            if f"/appartement/{slug}/" not in href:
                continue
            m = _CITYA_REF_RE.search(href)
            ref = m.group(1)
            if ref in seen_ref:
                continue
            seen_ref.add(ref)
            f = extract_fields(card.get_text(" ", strip=True), href)
            out.append({"source": "citya", "ref": ref,
                        "url": urljoin("https://www.citya.com", href), **f})
    return out


SCRAPERS = {
    "immocity": scrape_immocity,
    "foncia": scrape_foncia,
    "citya": scrape_citya,
}


def scrape_all(cfg):
    """Run every enabled scraper; never let one site's failure kill the rest."""
    results = []
    for name in cfg.get("sources", list(SCRAPERS)):
        fn = SCRAPERS.get(name)
        if not fn:
            continue
        try:
            found = fn(cfg)
            print(f"  [{name}] {len(found)} listing(s) on page")
            results.extend(found)
        except Exception as e:
            print(f"  [{name}] ERROR: {e}")
    return results
