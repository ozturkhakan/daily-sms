#!/usr/bin/env python3
"""
Galatasaray maçından 30 dakika önce SMS gönderir.
İçerik: maç bilgisi + kadro + dünya/Türkiye haberleri + altın fiyatı.
Sadece Nisan ayında çalışır.
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


# ── Football API ───────────────────────────────────────────────────────────
def _football(path: str, params: dict) -> dict:
    try:
        resp = requests.get(
            f"https://api-football-v1.p.rapidapi.com/v3/{path}",
            headers={
                "X-RapidAPI-Key": RAPIDAPI_KEY,
                "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com",
            },
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  Football API hatası ({path}): {e}")
        return {}


def find_upcoming_match() -> dict | None:
    """30 dakika içinde başlayacak Galatasaray maçını döner, yoksa None."""
    now   = datetime.now(timezone.utc)
    today = now.date()
    data  = _football("fixtures", {"team": GALA_ID, "date": str(today), "status": "NS"})
    for fixture in data.get("response", []):
        match_time    = datetime.fromisoformat(fixture["fixture"]["date"].replace("Z", "+00:00"))
        minutes_until = (match_time - now).total_seconds() / 60
        if 20 <= minutes_until <= 50:   # 30-dk cron için ±10 dk tolerans
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
            names = " | ".join(starters)
            return f"[{formation}] {names}" if formation else names
    return "Kadro henüz açıklanmadı"


# ── Haber ─────────────────────────────────────────────────────────────────
def fetch_rss(url: str, count: int) -> list[str]:
    try:
        feed = feedparser.parse(url)
        return [
            e.get("title", "").strip()
            for e in feed.entries[:count]
            if e.get("title", "").strip()
        ]
    except Exception as e:
        print(f"  RSS hatası: {e}")
        return []


def fetch_world_news() -> list[str]:
    items = fetch_rss("http://feeds.bbci.co.uk/news/world/rss.xml", 6)
    return items or fetch_rss("https://feeds.reuters.com/reuters/worldnews", 6)


def fetch_turkey_news() -> list[str]:
    items = fetch_rss("https://www.dailysabah.com/feeds/rss", 4)
    return items or fetch_rss("https://www.trtworld.com/rss/turkey", 4)


# ── Altın ─────────────────────────────────────────────────────────────────
def fetch_gold_price() -> str:
    try:
        xau_usd = float(
            requests.get("https://api.gold-api.com/price/XAU", timeout=10)
            .json()["price"]
        )
        usd_try = float(
            requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10)
            .json()["rates"]["TRY"]
        )
        gram_try = (xau_usd * usd_try) / 31.1035
        return f"{gram_try:,.0f} TL/gram  (USD/TRY: {usd_try:.2f})"
    except Exception as e:
        print(f"  Altın hatası: {e}")
        return "Veri alınamadı"


# ── Claude ile SMS metni ───────────────────────────────────────────────────
def build_sms(match: dict, lineup: str, world: list[str], turkey: list[str], gold: str) -> str:
    fixture    = match["fixture"]
    home       = match["teams"]["home"]["name"]
    away       = match["teams"]["away"]["name"]
    league     = match["league"]["name"]
    match_time = datetime.fromisoformat(fixture["date"].replace("Z", "+00:00"))
    tr_time    = match_time.astimezone(TURKEY_TZ).strftime("%H:%M")
    today_str  = datetime.now(TURKEY_TZ).strftime("%d %B %Y")

    raw = f"""
MAÇ BİLGİSİ:
{home} - {away} ({league}), {tr_time} TR
Kadro: {lineup}

DÜNYA HABERLERİ:
{chr(10).join(f"- {h}" for h in world)}

TÜRKİYE HABERLERİ:
{chr(10).join(f"- {h}" for h in turkey)}

ALTIN: {gold}
"""

    prompt = f"""İnternetsiz bir askere maç öncesi SMS özeti yaz. Türkçe yaz.
Tam olarak şu bölüm başlıklarını kullan (her biri kendi satırında):
[MAÇ] [KADRO] [DÜNYA] [TÜRKİYE] [ALTIN]

Kurallar:
- [MAÇ]: takımlar, lig, saat — tek satır
- [KADRO]: formasyon + oyuncular kısaltılmış soyadlarla, tek satır. "Henüz açıklanmadı" ise öyle yaz.
- [DÜNYA]: 4 haber, her biri tek kısa cümle
- [TÜRKİYE]: 3 haber, her biri tek kısa cümle
- [ALTIN]: gram TL + kur, tek satır
- Toplam 950 karakterin altında kal
- Son satır: --- {today_str}
- Gereksiz söz yok

Kaynak:
{raw}"""

    client = genai.Client(api_key=GEMINI_API_KEY)
    resp   = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
    return resp.text


# ── Twilio ─────────────────────────────────────────────────────────────────
def send_sms(body: str) -> str:
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    msg = client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=TO_PHONE_NUMBER)
    return msg.sid


# ── Ana Akış ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    now_tr  = datetime.now(TURKEY_TZ)

    # Sadece Nisan
    if now_tr.month != 4 and "--force" not in sys.argv:
        print(f"Nisan ayı değil ({now_tr.month}. ay), çıkılıyor.")
        sys.exit(0)

    print(f"=== {now_tr.strftime('%Y-%m-%d %H:%M')} TR — Maç kontrolü ===")

    match = find_upcoming_match()
    if not match and not dry_run:
        print("30 dakika içinde maç yok, çıkılıyor.")
        sys.exit(0)

    if match:
        fixture_id = match["fixture"]["id"]
        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]
        print(f"Maç bulundu: {home} - {away} (ID: {fixture_id})")
    else:
        fixture_id = 0
        print("Dry run — sahte maç verisiyle devam ediliyor.")
        match = {"fixture": {"id": 0, "date": now_tr.isoformat()},
                 "teams": {"home": {"name": "Galatasaray", "id": GALA_ID},
                            "away": {"name": "Rakip Takım", "id": 0}},
                 "league": {"name": "Test Ligi"}}

    print("Kadro çekiliyor...")
    lineup = fetch_lineup(fixture_id) if fixture_id else "Dry run kadrosu"

    print("Haberler çekiliyor...")
    world  = fetch_world_news()
    turkey = fetch_turkey_news()

    print("Altın fiyatı çekiliyor...")
    gold = fetch_gold_price()

    print("Claude ile SMS oluşturuluyor...")
    sms = build_sms(match, lineup, world, turkey, gold)

    print(f"\n{'='*50}")
    print(f"SMS ({len(sms)} karakter, ~{len(sms)//153 + 1} segment):")
    print(sms)
    print("=" * 50)

    if dry_run:
        print("\nDry run — SMS gönderilmedi.")
    else:
        print("\nSMS gönderiliyor...")
        sid = send_sms(sms)
        print(f"Gönderildi! SID: {sid}")
