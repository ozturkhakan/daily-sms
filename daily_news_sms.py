#!/usr/bin/env python3
"""
Günlük SMS servisi — haber + altın + Galatasaray + hava durumu.

Kullanım:
  python daily_news_sms.py news    [--force] [--dry-run]   Sabah haber bülteni
  python daily_news_sms.py match   [--force] [--dry-run]   Maç 30dk önce kadro
  python daily_news_sms.py weather [--force] [--dry-run]   Isparta hava durumu

Sadece Nisan ayında çalışır. --force ile ay kontrolü atlanır.
"""

import os
import sys
import time
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
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]

TURKEY_TZ = timezone(timedelta(hours=3))
GALA_ID   = 645

# Isparta koordinatlari
ISPARTA_LAT = 37.76
ISPARTA_LON = 30.55


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
        return f"{gram:,.0f}TL/g"
    except Exception as e:
        print(f"  Altin hatasi: {e}")
        return "??"


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
    """GS ilk 11'ini döner (sadece soyadlar)."""
    data = _football("fixtures/lineups", {"fixture": fixture_id})
    for team in data.get("response", []):
        if team["team"]["id"] != GALA_ID:
            continue
        formation = team.get("formation", "")
        names = [p["player"]["name"].split()[-1] for p in team.get("startXI", [])]
        if names:
            prefix = f"{formation} " if formation else ""
            return prefix + ",".join(names)
    return "Kadro belirsiz"


# ── Hava Durumu (Open-Meteo — ücretsiz, key gerekmez) ─────────────────────
WMO_CODES = {
    0: "Acik", 1: "Az bulutlu", 2: "Parcali bulutlu", 3: "Kapali",
    45: "Sisli", 48: "Sisli", 51: "Hafif ciseleme", 53: "Ciseleme",
    55: "Yogun ciseleme", 61: "Hafif yagmur", 63: "Yagmur",
    65: "Siddetli yagmur", 71: "Hafif kar", 73: "Kar", 75: "Yogun kar",
    80: "Saganak", 81: "Saganak", 82: "Siddetli saganak",
    95: "Gok gurultulu firtina", 96: "Dolu", 99: "Siddetli dolu",
}


def fetch_weather_isparta():
    """Isparta için bugün + yarın hava durumu verisini döner."""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": ISPARTA_LAT,
                "longitude": ISPARTA_LON,
                "daily": "temperature_2m_max,temperature_2m_min,weathercode,precipitation_sum",
                "timezone": "Europe/Istanbul",
                "forecast_days": 2,
            },
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()["daily"]
        days = []
        for i in range(2):
            code = d["weathercode"][i]
            days.append({
                "date": d["time"][i],
                "min": d["temperature_2m_min"][i],
                "max": d["temperature_2m_max"][i],
                "desc": WMO_CODES.get(code, f"Kod:{code}"),
                "rain": d["precipitation_sum"][i],
            })
        return days
    except Exception as e:
        print(f"  Hava durumu hatasi: {e}")
        return None


# ── Gemini AI ──────────────────────────────────────────────────────────────
GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]
RETRY_WAITS = [5, 15, 30]  # saniye — free tier throttle icin agresif backoff


def summarize_with_gemini(prompt):
    """Gemini API ile metin özetler. 429'da retry + fallback model dener."""
    for model in GEMINI_MODELS:
        for attempt, wait in enumerate(RETRY_WAITS):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                    timeout=15,
                )
                if r.status_code == 429:
                    print(f"  429 rate limit ({model}), {wait}s bekleniyor...")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                print(f"  Basarili: {model} (deneme {attempt + 1})")
                return text
            except Exception as e:
                print(f"  Gemini hatasi ({model}, deneme {attempt + 1}): {e}")
                break
        print(f"  {model} basarisiz, sonraki model deneniyor...")
    return None


# ── SMS Formatlama ─────────────────────────────────────────────────────────
MAX_SMS = 160  # tek segment GSM-7


def build_news_sms():
    """Sabah haber bülteni: dünya + Türkiye + altın — Gemini ile özetlenir."""
    print("Dunya haberleri cekiliyor...")
    world = fetch_world_news()
    print(f"  {len(world)} baslik")

    print("Turkiye haberleri cekiliyor...")
    turkey = fetch_turkey_news()
    print(f"  {len(turkey)} baslik")

    print("Altin fiyati cekiliyor...")
    gold = fetch_gold_price()
    print(f"  {gold}")

    headlines = "\n".join(world[:3] + turkey[:2])
    prompt = (
        f"Asagidaki haber basliklarini ve altin fiyatini tek bir Turkce SMS'e ozetle. "
        f"SMS en fazla {MAX_SMS} karakter olmali. Sadece SMS metnini yaz, baska bir sey yazma. "
        f"Turkce karakterler kullanma (c, g, i, o, s, u kullan). Kisa ve net cumle kur.\n\n"
        f"Basliklar:\n{headlines}\n\nAltin: {gold}"
    )
    print("Gemini ile ozetleniyor...")
    sms = summarize_with_gemini(prompt)
    if sms and len(sms) <= MAX_SMS:
        return sms
    if sms:
        return sms[:MAX_SMS]
    # fallback: AI basarisiz olursa ham baslik
    print("  Fallback: ham basliklar kullaniliyor")
    lines = [h[:50] for h in (world[:2] + turkey[:1])]
    lines.append(f"Altin:{gold}")
    return "\n".join(lines)[:MAX_SMS]


def build_match_sms():
    """Maç öncesi uyarı — Gemini ile özetlenir."""
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

    print(f"  Mac: {home} - {away}")
    print("Kadro cekiliyor...")
    lineup = fetch_lineup(fixture_id)
    print(f"  {lineup[:60]}...")

    prompt = (
        f"Asagidaki mac bilgisini tek bir Turkce SMS'e ozetle. "
        f"SMS en fazla {MAX_SMS} karakter olmali. Sadece SMS metnini yaz. "
        f"Turkce karakterler kullanma (c, g, i, o, s, u kullan).\n\n"
        f"Mac: {home} vs {away}\nLig: {league}\nSaat: {tr_time}\nKadro: {lineup}"
    )
    print("Gemini ile ozetleniyor...")
    sms = summarize_with_gemini(prompt)
    if sms and len(sms) <= MAX_SMS:
        return sms
    if sms:
        return sms[:MAX_SMS]
    # fallback
    print("  Fallback: ham bilgi kullaniliyor")
    lines = [f"MAC {tr_time}", f"{home}-{away}", lineup]
    return "\n".join(lines)[:MAX_SMS]


def build_weather_sms():
    """Isparta hava durumu — Gemini ile özetlenir."""
    print("Isparta hava durumu cekiliyor...")
    days = fetch_weather_isparta()
    if not days:
        print("  Veri alinamadi.")
        return None

    raw = ""
    for d in days:
        raw += f"{d['date']}: {d['desc']}, {d['min']:.0f}-{d['max']:.0f}C, yagis {d['rain']:.1f}mm\n"
    print(f"  {raw.strip()}")

    prompt = (
        f"Asagidaki hava durumu verisini Isparta icin tek bir Turkce SMS'e ozetle. "
        f"SMS en fazla {MAX_SMS} karakter olmali. Sadece SMS metnini yaz, baska bir sey yazma. "
        f"Turkce karakterler kullanma (c, g, i, o, s, u kullan). "
        f"Bugun ve yarin icin sicaklik ve durumu kisa yaz.\n\n{raw}"
    )
    print("Gemini ile ozetleniyor...")
    sms = summarize_with_gemini(prompt)
    if sms and len(sms) <= MAX_SMS:
        return sms
    if sms:
        return sms[:MAX_SMS]
    # fallback
    print("  Fallback: ham veri kullaniliyor")
    lines = []
    for d in days:
        lines.append(f"{d['date'][5:]}: {d['desc']} {d['min']:.0f}/{d['max']:.0f}C")
    return ("Isparta " + ", ".join(lines))[:MAX_SMS]


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
    modes = {"news", "match", "weather"}
    mode = next((m for m in modes if m in args), None)
    dry_run = "--dry-run" in args
    force = "--force" in args
    now_tr = datetime.now(TURKEY_TZ)

    if not mode:
        print("Kullanim: python daily_news_sms.py [news|match|weather] [--dry-run] [--force]")
        sys.exit(1)

    if now_tr.month != 4 and not force:
        print(f"Nisan degil ({now_tr.month}. ay), cikiliyor.")
        sys.exit(0)

    print(f"=== {now_tr.strftime('%Y-%m-%d %H:%M')} TR — mod: {mode} ===\n")

    builders = {"news": build_news_sms, "match": build_match_sms, "weather": build_weather_sms}
    sms = builders[mode]()

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
