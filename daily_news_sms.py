#!/usr/bin/env python3
"""
Günlük SMS servisi — haber + altın + Galatasaray.

Kullanım:
  python daily_news_sms.py news  [--force] [--dry-run]   Sabah haber bülteni
  python daily_news_sms.py match [--force] [--dry-run]   Maç 30dk önce kadro

Sadece Nisan ayında çalışır. --force ile ay kontrolü atlanır.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from twilio.rest import Client

# ── Config ─────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]
TO_PHONE_NUMBER    = os.environ["TO_PHONE_NUMBER"]
RAPIDAPI_KEY       = os.environ["RAPIDAPI_KEY"]

TURKEY_TZ = timezone(timedelta(hours=3))
GALA_ID   = 645


# ── RSS Haber ──────────────────────────────────────────────────────────────
def fetch_rss(url, count=5):
    """RSS feed'den başlıkları çeker. Hata olursa boş liste döner."""
    try:
        feed = feedparser.parse(url)
        return [
            e.get("title", "").strip()
            for e in feed.entries[:count]
            if e.get("title", "").strip()
        ]
    except Exception as e:
        print(f"  RSS hatasi ({url}): {e}")
        return []


def fetch_world_news():
    return (fetch_rss("https://www.ntv.com.tr/dunya.rss", 5)
            or fetch_rss("http://feeds.bbci.co.uk/news/world/rss.xml", 5))


def fetch_turkey_news():
    return (fetch_rss("https://www.ntv.com.tr/turkiye.rss", 4)
            or fetch_rss("http://feeds.bbci.co.uk/news/world/rss.xml", 4))


# ── Altın ──────────────────────────────────────────────────────────────────
def fetch_gold_price():
    """Gram altın TL fiyatını döner. İki ücretsiz API, key gerekmez."""
    try:
        r1 = requests.get("https://api.gold-api.com/price/XAU", timeout=10)
        r1.raise_for_status()
        xau_usd = float(r1.json()["price"])

        r2 = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10)
        r2.raise_for_status()
        usd_try = float(r2.json()["rates"]["TRY"])

        gram = (xau_usd * usd_try) / 31.1035
        return f"{gram:,.0f} TL/gram (USD/TRY: {usd_try:.2f})"
    except Exception as e:
        print(f"  Altin hatasi: {e}")
        return "Veri alinamadi"


# ── Football API ───────────────────────────────────────────────────────────
def _football(path, params):
    """api-football RapidAPI çağrısı. Hata olursa boş dict döner."""
    try:
        r = requests.get(
            f"https://api-football-v1.p.rapidapi.com/v3/{path}",
            headers={
                "X-RapidAPI-Key": RAPIDAPI_KEY,
                "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com",
            },
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Football API hatasi ({path}): {e}")
        return {}


def find_upcoming_match():
    """20-50 dakika içinde başlayacak GS maçını döner, yoksa None."""
    now = datetime.now(timezone.utc)
    data = _football("fixtures", {
        "team": GALA_ID,
        "date": str(now.date()),
        "status": "NS",
    })
    for fix in data.get("response", []):
        match_time = datetime.fromisoformat(
            fix["fixture"]["date"].replace("Z", "+00:00")
        )
        mins = (match_time - now).total_seconds() / 60
        if 20 <= mins <= 50:
            return fix
    return None


def fetch_lineup(fixture_id):
    """GS ilk 11'ini döner."""
    data = _football("fixtures/lineups", {"fixture": fixture_id})
    for team in data.get("response", []):
        if team["team"]["id"] != GALA_ID:
            continue
        formation = team.get("formation", "")
        names = [p["player"]["name"] for p in team.get("startXI", [])]
        if names:
            prefix = f"[{formation}] " if formation else ""
            return prefix + ", ".join(names)
    return "Kadro henuz aciklanmadi"


# ── SMS Formatlama ─────────────────────────────────────────────────────────
def build_news_sms():
    """Sabah haber bülteni: dünya + Türkiye + altın."""
    print("Dunya haberleri cekiliyor...")
    world = fetch_world_news()
    print(f"  {len(world)} baslik")

    print("Turkiye haberleri cekiliyor...")
    turkey = fetch_turkey_news()
    print(f"  {len(turkey)} baslik")

    print("Altin fiyati cekiliyor...")
    gold = fetch_gold_price()
    print(f"  {gold}")

    today = datetime.now(TURKEY_TZ).strftime("%d/%m/%Y")
    lines = [f"GUNLUK BULTEN {today}", ""]

    lines.append("[DUNYA]")
    for h in world[:3]:
        lines.append(f"- {h[:60]}")
    lines.append("")

    lines.append("[TURKIYE]")
    for h in turkey[:2]:
        lines.append(f"- {h[:60]}")
    lines.append("")

    lines.append(f"[ALTIN] {gold}")

    return "\n".join(lines)


def build_match_sms():
    """Maç öncesi uyarı: maç bilgisi + kadro."""
    print("Mac kontrolu...")
    match = find_upcoming_match()
    if not match:
        print("30 dk icinde mac yok.")
        return None

    fixture_id = match["fixture"]["id"]
    home = match["teams"]["home"]["name"]
    away = match["teams"]["away"]["name"]
    league = match["league"]["name"]
    match_time = datetime.fromisoformat(
        match["fixture"]["date"].replace("Z", "+00:00")
    )
    tr_time = match_time.astimezone(TURKEY_TZ).strftime("%H:%M")
    today = datetime.now(TURKEY_TZ).strftime("%d/%m/%Y")

    print(f"  Mac: {home} - {away}")
    print("Kadro cekiliyor...")
    lineup = fetch_lineup(fixture_id)
    print(f"  {lineup[:60]}...")

    lines = [
        f"MAC UYARISI {today}",
        "",
        f"{home} - {away}",
        f"{league}, saat {tr_time}",
        "",
        f"[KADRO]",
        lineup,
    ]
    return "\n".join(lines)


# ── Twilio ─────────────────────────────────────────────────────────────────
def send_sms(body):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    msg = client.messages.create(
        body=body,
        from_=TWILIO_FROM_NUMBER,
        to=TO_PHONE_NUMBER,
    )
    return msg.sid


# ── Ana Akış ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    mode = "news" if "news" in args else ("match" if "match" in args else None)
    dry_run = "--dry-run" in args
    force = "--force" in args
    now_tr = datetime.now(TURKEY_TZ)

    if not mode:
        print("Kullanim: python daily_news_sms.py [news|match] [--dry-run] [--force]")
        sys.exit(1)

    if now_tr.month != 4 and not force:
        print(f"Nisan degil ({now_tr.month}. ay), cikiliyor.")
        sys.exit(0)

    print(f"=== {now_tr.strftime('%Y-%m-%d %H:%M')} TR — mod: {mode} ===\n")

    sms = build_news_sms() if mode == "news" else build_match_sms()

    if sms is None:
        sys.exit(0)

    print(f"\n{'=' * 50}")
    print(f"SMS ({len(sms)} karakter):\n")
    print(sms)
    print("\n" + "=" * 50)

    if dry_run:
        print("\nDry run — SMS gonderilmedi.")
    else:
        print("\nSMS gonderiliyor...")
        sid = send_sms(sms)
        print(f"Gonderildi! SID: {sid}")
