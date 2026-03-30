#!/usr/bin/env python3
"""
İki mod:
  python daily_news_sms.py news   → sabah haber + altın özeti (her gün)
  python daily_news_sms.py match  → maç 30 dk sonraysa kadro + uyarı
Sadece Nisan ayında çalışır (--force ile atlanır).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from google import genai
from twilio.rest import Client

# ── Config ─────────────────────────────────────────────────────────────────
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]
TO_PHONE_NUMBER    = os.environ["TO_PHONE_NUMBER"]
RAPIDAPI_KEY       = os.environ["RAPIDAPI_KEY"]

TURKEY_TZ = timezone(timedelta(hours=3))
GALA_ID   = 645


# ── Yardımcı ───────────────────────────────────────────────────────────────
def gemini(prompt: str) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)
    resp   = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
    return resp.text


def send_sms(body: str) -> str:
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    msg = client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=TO_PHONE_NUMBER)
    return msg.sid


# ── Haber ─────────────────────────────────────────────────────────────────
def fetch_rss(url: str, count: int) -> list[str]:
    try:
        feed = feedparser.parse(url)
        return [e.get("title", "").strip() for e in feed.entries[:count] if e.get("title", "").strip()]
    except Exception as e:
        print(f"  RSS hatası: {e}")
        return []


def fetch_world_news() -> list[str]:
    return fetch_rss("http://feeds.bbci.co.uk/news/world/rss.xml", 6) or \
           fetch_rss("https://feeds.reuters.com/reuters/worldnews", 6)


def fetch_turkey_news() -> list[str]:
    return fetch_rss("https://www.dailysabah.com/feeds/rss", 4) or \
           fetch_rss("https://www.trtworld.com/rss/turkey", 4)


# ── Altın ─────────────────────────────────────────────────────────────────
def fetch_gold_price() -> str:
    try:
        xau_usd  = float(requests.get("https://api.gold-api.com/price/XAU", timeout=10).json()["price"])
        usd_try  = float(requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10).json()["rates"]["TRY"])
        gram_try = (xau_usd * usd_try) / 31.1035
        return f"{gram_try:,.0f} TL/gram  (USD/TRY: {usd_try:.2f})"
    except Exception as e:
        print(f"  Altın hatası: {e}")
        return "Veri alınamadı"


# ── Football API ───────────────────────────────────────────────────────────
def _football(path: str, params: dict) -> dict:
    try:
        resp = requests.get(
            f"https://api-football-v1.p.rapidapi.com/v3/{path}",
            headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com"},
            params=params, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  Football API hatası: {e}")
        return {}


def find_upcoming_match() -> dict | None:
    now  = datetime.now(timezone.utc)
    data = _football("fixtures", {"team": GALA_ID, "date": str(now.date()), "status": "NS"})
    for fixture in data.get("response", []):
        match_time    = datetime.fromisoformat(fixture["fixture"]["date"].replace("Z", "+00:00"))
        minutes_until = (match_time - now).total_seconds() / 60
        if 20 <= minutes_until <= 50:
            return fixture
    return None


def fetch_lineup(fixture_id: int) -> str:
    data = _football("fixtures/lineups", {"fixture": fixture_id})
    for team in data.get("response", []):
        if team["team"]["id"] != GALA_ID:
            continue
        formation = team.get("formation", "")
        starters  = [p["player"]["name"] for p in team.get("startXI", [])]
        if starters:
            return f"[{formation}] " + " | ".join(starters)
    return "Kadro henüz açıklanmadı"


# ── MOD 1: Sabah haber bülteni ─────────────────────────────────────────────
def run_news():
    print("Haberler çekiliyor...")
    world  = fetch_world_news()
    turkey = fetch_turkey_news()
    gold   = fetch_gold_price()
    today  = datetime.now(TURKEY_TZ).strftime("%d %B %Y")

    raw = (
        "DÜNYA:\n" + "\n".join(f"- {h}" for h in world) + "\n\n"
        "TÜRKİYE:\n" + "\n".join(f"- {h}" for h in turkey) + "\n\n"
        f"ALTIN: {gold}"
    )

    prompt = f"""İnternetsiz bir askere Türkçe sabah haber özeti SMS yaz.
Bölüm başlıkları (her biri kendi satırında): [DÜNYA] [TÜRKİYE] [ALTIN]
- [DÜNYA]: 4 haber, her biri tek kısa cümle
- [TÜRKİYE]: 3 haber, her biri tek kısa cümle
- [ALTIN]: gram TL + kur, tek satır
- Toplam 900 karakterin altında
- Son satır: --- {today}
- Gereksiz söz yok

Kaynak:
{raw}"""

    print("Gemini ile özet oluşturuluyor...")
    return gemini(prompt)


# ── MOD 2: Maç öncesi uyarı ────────────────────────────────────────────────
def run_match():
    print("Maç kontrolü...")
    match = find_upcoming_match()
    if not match:
        print("30 dakika içinde maç yok, çıkılıyor.")
        return None

    fixture_id = match["fixture"]["id"]
    home       = match["teams"]["home"]["name"]
    away       = match["teams"]["away"]["name"]
    league     = match["league"]["name"]
    match_time = datetime.fromisoformat(match["fixture"]["date"].replace("Z", "+00:00"))
    tr_time    = match_time.astimezone(TURKEY_TZ).strftime("%H:%M")
    today      = datetime.now(TURKEY_TZ).strftime("%d %B %Y")

    print(f"Maç: {home} - {away}, kadro çekiliyor...")
    lineup = fetch_lineup(fixture_id)

    prompt = f"""İnternetsiz bir askere Türkçe maç öncesi SMS uyarısı yaz.
Bölüm başlıkları (her biri kendi satırında): [MAÇ] [KADRO]
- [MAÇ]: takımlar, lig, saat — tek satır
- [KADRO]: formasyon + oyuncu soyadları, tek satır. Açıklanmadıysa öyle yaz.
- Toplam 400 karakterin altında
- Son satır: --- {today}

Bilgiler:
{home} - {away} ({league}), {tr_time} TR
Kadro: {lineup}"""

    print("Gemini ile özet oluşturuluyor...")
    return gemini(prompt)


# ── Ana Akış ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args    = sys.argv[1:]
    mode    = "news" if "news" in args else "match" if "match" in args else None
    dry_run = "--dry-run" in args
    now_tr  = datetime.now(TURKEY_TZ)

    if not mode:
        print("Kullanım: python daily_news_sms.py [news|match] [--dry-run] [--force]")
        sys.exit(1)

    if now_tr.month != 4 and "--force" not in args:
        print(f"Nisan değil ({now_tr.month}. ay), çıkılıyor.")
        sys.exit(0)

    print(f"=== {now_tr.strftime('%Y-%m-%d %H:%M')} TR — mod: {mode} ===\n")

    sms = run_news() if mode == "news" else run_match()

    if sms is None:
        sys.exit(0)

    print(f"\n{'='*50}")
    print(f"SMS ({len(sms)} karakter):\n{sms}")
    print("=" * 50)

    if dry_run:
        print("\nDry run — SMS gönderilmedi.")
    else:
        print("\nSMS gönderiliyor...")
        sid = send_sms(sms)
        print(f"Gönderildi! SID: {sid}")
