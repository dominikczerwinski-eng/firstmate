#!/usr/bin/env python3
"""Parse Polish hotel requests into a small, privacy-safe intent record."""
import json
import re
import sys
import unicodedata
from datetime import date, timedelta


def norm(text):
    return " ".join((text or "").strip().split())


def number(pattern, text, default=None):
    match = re.search(pattern, text, re.I)
    return int(match.group(1)) if match else default


def next_weekday(start: date, weekday: int) -> date:
    d = start
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def parse(text):
    text = norm(text)
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)
    )
    location = None
    for pattern in (
        r"(?:adres|address|miasto)\s*[:=-]?\s*([^,;\n]+)",
        r"(?:hotel|nocleg)\s+(?:w|we)\s+([^,;\n]+)",
        # "hotel dzisiaj, Berlin Mitte, 1 noc"
        r"hotel\s+(?:dzisiaj|dzis|dziś|jutro|poniedzialek|poniedziałek|wtorek|sroda|środa|czwartek|piatek|piątek|sobota|niedziela)?\s*,\s*([^,;\n]+)",
        r"hotel\s+[^,\n]*,\s*([A-Za-zÀ-ž][^,;\n]{2,})",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            cand = norm(re.sub(r"\s+\d{1,2}[./-].*$", "", match.group(1)))
            # drop pure relative day words
            if cand and not re.fullmatch(
                r"(dzisiaj|dzis|dziś|jutro|poniedzialek|poniedziałek|wtorek|sroda|środa|czwartek|piatek|piątek|sobota|niedziela|blisko.*)",
                cand,
                re.I,
            ):
                location = cand
                break

    customer = None
    match = re.search(r"(?:klient|firma)\s*[:=-]?\s*([^,;\n]+)", text, re.I)
    if match:
        customer = norm(match.group(1))
    if not customer:
        match = re.search(r"blisko\s+(?:klienta\s+)?([^,;\n]+)", text, re.I)
        if match:
            customer = norm(match.group(1))

    budget = number(
        r"(?:okolo|około|budzet|budżet|do)\s*(?:[~≈])?\s*(\d{2,5})\s*(?:pln|zl|zł|eur|euro)?",
        folded,
    )
    nights = number(r"(\d+)\s*(?:noc|noce|nocy|night|nights)", folded, 1)
    guests = number(r"(\d+)\s*(?:osob|osoby|osób|guest|guests)", folded, 1)
    score_match = re.search(r"(?:ocena|score|minimum)\s*[>=:]?\s*(\d+(?:[.,]\d+)?)", folded)
    score = float(score_match.group(1).replace(",", ".")) if score_match else 8.0
    dates = re.findall(r"\b(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)\b", text)

    checkin = dates[0] if dates else None
    checkout = dates[1] if len(dates) > 1 else None
    today = date.today()
    if not checkin:
        if re.search(r"\bdzisiaj|\bdzis\b|\bdziś\b", folded):
            d0 = today
        elif re.search(r"\bjutro\b", folded):
            d0 = today + timedelta(days=1)
        elif re.search(r"\bwtorek\b", folded):
            d0 = next_weekday(today, 1)
            if d0 == today:
                d0 += timedelta(days=7)
        else:
            d0 = today + timedelta(days=1)
        checkin = d0.isoformat()
        checkout = (d0 + timedelta(days=max(1, nights or 1))).isoformat()

    return {
        "text": text,
        "location": location,
        "customer_query": customer,
        "checkin": checkin,
        "checkout": checkout,
        "nights": nights,
        "guests": guests,
        "budget_pln": budget or 400,
        "score_min": score,
        "breakfast": True,  # default policy
        "parking": True,
        "payment": "card",
        "radius_km": number(r"(?:promien|promień|radius|km)\s*[<=:]?\s*(\d+)", folded, 5),
        "provider": "booking",
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: fm-hotel-parse.py TEXT")
    print(json.dumps(parse(sys.argv[1]), ensure_ascii=False, sort_keys=True))
