#!/usr/bin/env python3
"""Build 2-3 Booking hotel candidates near a place (OSM + Booking deep links)."""
from __future__ import annotations

import json
import re as _re
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

UA = "FenedoHotelBot/1.0 (+local firstmate hotel shortlist)"
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)


def http_json(url: str, data: bytes | None = None, timeout: int = 25) -> dict | list:
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def re_berlin_mitte(place: str) -> bool:
    return bool(_re.search(r"berlin\s*mitte", place or "", _re.I))


def geocode(place: str) -> tuple[float, float, str]:
    q = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
    rows = http_json("https://nominatim.openstreetmap.org/search?" + q)
    if not rows:
        raise SystemExit("geocode_failed:" + place)
    row = rows[0]
    return float(row["lat"]), float(row["lon"]), str(row.get("display_name") or place)


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def overpass_hotels(lat: float, lon: float, radius_m: int = 1500) -> list[dict]:
    query = (
        f'[out:json][timeout:15];'
        f'node["tourism"="hotel"](around:{radius_m},{lat},{lon});'
        f'out body 30;'
    )
    body = urllib.parse.urlencode({"data": query}).encode()
    data: dict | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            data = http_json(endpoint, data=body, timeout=20)  # type: ignore[assignment]
            break
        except Exception:
            continue
    if data is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        stars = tags.get("stars")
        try:
            stars_n = float(str(stars).replace(",", ".")) if stars is not None else None
        except ValueError:
            stars_n = None
        out.append(
            {
                "name": name,
                "lat": float(el.get("lat") or 0),
                "lon": float(el.get("lon") or 0),
                "stars": stars_n,
                "website": tags.get("website") or tags.get("contact:website"),
            }
        )
    return out


def booking_url(hotel_name: str, place: str, checkin: str, checkout: str) -> str:
    ss = f"{hotel_name}, {place}"
    return "https://www.booking.com/searchresults.pl.html?" + urllib.parse.urlencode(
        {
            "ss": ss,
            "checkin": checkin,
            "checkout": checkout,
            "group_adults": 1,
            "no_rooms": 1,
            "selected_currency": "EUR",
            "nflt": "review_score=80;mealplan=1",
        }
    )


def shortlist(
    place: str,
    checkin: str | None,
    checkout: str | None,
    radius_km: float = 3.0,
    limit: int = 3,
) -> list[dict]:
    today = date.today()
    if not checkin:
        checkin = today.isoformat()
    if not checkout:
        checkout = (today + timedelta(days=1)).isoformat()
    lat, lon, label = geocode(place)
    # Berlin Mitte: Nominatim sometimes lands west of core hotel density.
    if re_berlin_mitte(place):
        lat, lon = 52.5200, 13.4050
    hotels: list[dict] = []
    for r_km in (1.5, 2.5, 4.0, max(radius_km, 5.0)):
        try:
            hotels = overpass_hotels(lat, lon, radius_m=int(r_km * 1000))
        except Exception:
            hotels = []
        if hotels:
            break
    for h in hotels:
        h["distance_km"] = round(haversine_km(lat, lon, h["lat"], h["lon"]), 2)
        h["url"] = booking_url(h["name"], place, checkin, checkout)
        h["score"] = h["stars"]
        h["price_pln"] = "?"
        h["place_label"] = label
        h["checkin"] = checkin
        h["checkout"] = checkout
    hotels.sort(key=lambda h: (0 if h.get("stars") else 1, h.get("distance_km") or 99, h["name"]))
    return hotels[:limit]


def render_slack(rows: list[dict], place: str) -> str:
    if not rows:
        return (
            f"Nie znalazłem nazwanych hoteli koło *{place}* na mapie. "
            "Podaj inne miasto/adres albo wklej linki Booking."
        )
    checkin = rows[0].get("checkin")
    checkout = rows[0].get("checkout")
    lines = [
        f"Propozycje *Booking* dla *{place}* ({checkin} → {checkout}) — wybierz *1 / 2 / 3*:",
        "",
    ]
    for i, row in enumerate(rows, 1):
        stars = f", gwiazdki {row['stars']:g}" if row.get("stars") else ""
        lines.append(
            f"{i}. *{row['name']}* — ok. {row.get('distance_km', '?')} km{stars}\n"
            f"   <{row['url']}|Booking – sprawdź cenę, ocenę ≥8, śniadanie, parking, kartę>"
        )
    lines += [
        "",
        "*Sztywne kryteria (potwierdź na karcie Booking przed wyborem):*",
        "śniadanie · parking (płatny OK) · ocena ≥ 8 · płatność kartą · do ~400 zł · tylko Booking",
        "",
        "Napisz *1*, *2* albo *3*. Domknę pakiet Awios→Booking dla Dominika (gość: Sebastian Waloch). Bez auto-płatności.",
        "_Źródło nazw: mapa OSM w okolicy; cena/ocena/live dostępność = Booking (bez oficjalnego API)._",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: fm-hotel-shortlist.py PLACE [CHECKIN] [CHECKOUT] [--json]")
    as_json = "--json" in sys.argv[1:]
    args = [a for a in sys.argv[1:] if a != "--json"]
    place = args[0]
    checkin = args[1] if len(args) > 1 else None
    checkout = args[2] if len(args) > 2 else None
    rows = shortlist(place, checkin, checkout)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(render_slack(rows, place))
