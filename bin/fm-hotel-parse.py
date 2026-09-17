#!/usr/bin/env python3
"""Parse Polish hotel requests into a small, privacy-safe intent record."""
import json
import re
import sys
import unicodedata


def norm(text):
    return " ".join((text or "").strip().split())


def number(pattern, text, default=None):
    match = re.search(pattern, text, re.I)
    return int(match.group(1)) if match else default


def parse(text):
    text = norm(text)
    folded = "".join(c for c in unicodedata.normalize("NFKD", text.lower())
                    if not unicodedata.combining(c))
    location = None
    for pattern in (r"(?:adres|address|w|w miejscowosci|miasto)\s*[:=-]?\s*([^,;\n]+)",
                    r"(?:hotel|nocleg)\s+(?:w|we)\s+([^,;\n]+)"):
        match = re.search(pattern, text, re.I)
        if match:
            location = norm(re.sub(r"\s+\d{1,2}[./-]\d.*$", "", match.group(1)))
            break
    customer = None
    match = re.search(r"(?:klient|firma)\s*[:=-]?\s*([^,;\n]+)", text, re.I)
    if match:
        customer = norm(match.group(1))
    if not customer:
        match = re.search(r"dla\s*[:=-]?\s*([^,;\n]+)", text, re.I)
        if match:
            customer = norm(match.group(1))
    if not customer:
        match = re.search(r"(?:po nazwie|po kliencie)\s*[:=-]?\s*([^,;\n]+)", text, re.I)
        if match:
            customer = norm(match.group(1))
    budget = number(r"(?:okolo|około|budzet|budżet|do)\s*(?:[~≈])?\s*(\d{2,5})\s*(?:pln|zl|zł)?", folded)
    nights = number(r"(\d+)\s*(?:noc|noce|nocy|night|nights)", folded, 1)
    guests = number(r"(\d+)\s*(?:osob|osoby|osób|guest|guests)", folded, 1)
    score_match = re.search(r"(?:ocena|score|minimum)\s*[>=:]?\s*(\d+(?:[.,]\d+)?)", folded)
    score = float(score_match.group(1).replace(",", ".")) if score_match else 8.0
    dates = re.findall(r"\b(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)\b", text)
    return {
        "text": text,
        "location": location,
        "customer_query": customer,
        "checkin": dates[0] if dates else None,
        "checkout": dates[1] if len(dates) > 1 else None,
        "nights": nights,
        "guests": guests,
        "budget_pln": budget or 400,
        "score_min": score,
        "breakfast": bool(re.search(r"sniad|śniad|breakfast", folded)),
        "parking": bool(re.search(r"parking", folded)),
        "payment": "card",
        "radius_km": number(r"(?:promien|promień|radius|km)\s*[<=:]?\s*(\d+)", folded, 5),
        "provider": "booking",
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: fm-hotel-parse.py TEXT")
    print(json.dumps(parse(sys.argv[1]), ensure_ascii=False, sort_keys=True))
