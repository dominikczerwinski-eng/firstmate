#!/usr/bin/env python3
"""Save hotel invoice attachments from Slack into iCloud monthly purchase folders.

Folder layout (existing captain convention):
  .../YYYY faktury zakupowe + wyciągi YYYY/MM:YYYY/

Invoice issue date (from PDF text) chooses MM:YYYY; fallback = today.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

UA = "FenedoHotelInvoice/1.0"
DEFAULT_ROOT = Path.home() / (
    "Library/Mobile Documents/com~apple~CloudDocs/"
    "Fenedo sp. z o.o./Finanse/Wyniki"
)


def load_env(home: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    p = home / ".env"
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def year_base(root: Path, year: int) -> Path:
    # Prefer an existing on-disk folder (macOS often stores NFD names in iCloud).
    for child in root.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if str(year) in name and "faktury" in name.casefold():
            return child
    # Create only if missing entirely (NFC spelling; rare).
    direct = root / f"{year} faktury zakupowe + wyciągi {year}"
    direct.mkdir(parents=True, exist_ok=True)
    return direct


def month_dir(root: Path, d: date) -> Path:
    base = year_base(root, d.year)
    folder = base / f"{d.month:02d}:{d.year}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def extract_invoice_date(path: Path) -> date | None:
    text = ""
    if path.suffix.lower() == ".pdf" and shutil.which("pdftotext"):
        try:
            text = subprocess.check_output(
                ["pdftotext", "-layout", "-q", str(path), "-"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
        except Exception:
            text = ""
    if not text:
        return None
    # Prefer labeled invoice dates
    patterns = [
        r"(?:invoice\s*date|date\s*of\s*issue|data\s*wystawienia|rechnungsdatum|datum)\s*[:.]?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})",
        r"(?:invoice\s*date|date\s*of\s*issue|data\s*wystawienia)\s*[:.]?\s*(\d{4})[./-](\d{1,2})[./-](\d{1,2})",
        r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b",
        r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g[0]) == 4:  # yyyy mm dd
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            else:
                d, mo, y = int(g[0]), int(g[1]), int(g[2])
                if y < 100:
                    y += 2000
            return date(y, mo, d)
        except ValueError:
            continue
    return None


def safe_name(original: str, when: date) -> str:
    base = Path(original).name
    base = re.sub(r"[^\w.\- ()ąęśćżółńĄĘŚĆŻÓŁŃ]+", "_", base, flags=re.U)
    base = base.strip("._ ") or "invoice.pdf"
    if not Path(base).suffix:
        base += ".pdf"
    return f"{when.isoformat()}_hotel_{base}"


def download_slack_file(token: str, file_obj: dict, dest: Path) -> Path:
    url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if not url:
        raise SystemExit("no_download_url")
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    dest.write_bytes(data)
    return dest


def slack_api(token: str, method: str, **params):
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def process_message(home: Path, token: str, channel: str, msg: dict, finance_root: Path) -> list[str]:
    files = msg.get("files") or []
    # files may only be ids in history — enrich
    out_notes = []
    for f in files:
        fobj = f
        if "url_private" not in fobj and fobj.get("id"):
            info = slack_api(token, "files.info", file=fobj["id"])
            if not info.get("ok"):
                out_notes.append(f"plik {fobj.get('id')}: {info.get('error')}")
                continue
            fobj = info.get("file") or fobj
        name = fobj.get("name") or "invoice.bin"
        mimetype = (fobj.get("mimetype") or "").lower()
        if not (
            mimetype in ("application/pdf", "image/jpeg", "image/png", "image/heic")
            or name.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".heic"))
        ):
            out_notes.append(f"pominięto {name} (nie PDF/obraz)")
            continue
        tmp = home / "state" / "hotel" / "inbox"
        tmp.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp / name
        try:
            download_slack_file(token, fobj, tmp_path)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            if "missing_scope" in err or "not_allowed" in err:
                out_notes.append(
                    "Brak uprawnień Slack files:read / files:read.remote — dodaj scope i reinstall Fenka."
                )
            else:
                out_notes.append(f"pobieranie {name} nieudane: {exc}")
            continue
        inv_date = extract_invoice_date(tmp_path) or date.today()
        dest_dir = month_dir(finance_root, inv_date)
        dest_name = safe_name(name, inv_date)
        dest = dest_dir / dest_name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{dest_name.rsplit('.',1)[0]}_{n}.{dest_name.rsplit('.',1)[-1]}"
            n += 1
        shutil.move(str(tmp_path), str(dest))
        rel = str(dest)
        out_notes.append(
            f"Zapisano fakturę → `{dest_dir.name}/{dest.name}` "
            f"(data wystawienia: {inv_date.isoformat()})"
        )
        # journal
        journal = home / "state" / "hotel" / "invoices.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "saved_at": datetime.now().isoformat(timespec="seconds"),
                        "invoice_date": inv_date.isoformat(),
                        "path": rel,
                        "slack_ts": msg.get("ts"),
                        "slack_user": msg.get("user"),
                        "original_name": name,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return out_notes


def main() -> int:
    home = Path(os.environ.get("FM_HOME") or Path(__file__).resolve().parents[1])
    env = load_env(home)
    token = env.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
    channel = env.get("FM_HOTEL_CHANNEL_ID") or os.environ.get("FM_HOTEL_CHANNEL_ID")
    if not token or not channel:
        print("need SLACK_BOT_TOKEN and FM_HOTEL_CHANNEL_ID", file=sys.stderr)
        return 2
    finance_root = Path(
        env.get("HOTEL_INVOICE_ROOT")
        or os.environ.get("HOTEL_INVOICE_ROOT")
        or DEFAULT_ROOT
    )
    if not finance_root.is_dir():
        print("finance root missing: " + str(finance_root), file=sys.stderr)
        return 2

    state = home / "state" / "hotel"
    state.mkdir(parents=True, exist_ok=True)
    cursor_file = state / "invoice.cursor"
    oldest = cursor_file.read_text().strip() if cursor_file.exists() else ""
    params = {"channel": channel, "limit": "50"}
    if oldest:
        params["oldest"] = oldest
    data = slack_api(token, "conversations.history", **params)
    if not data.get("ok"):
        print("slack:" + str(data.get("error")), file=sys.stderr)
        return 1
    messages = sorted(data.get("messages") or [], key=lambda m: m.get("ts") or "")
    handled_file = state / "invoice-handled-ts.txt"
    handled = set(handled_file.read_text().split()) if handled_file.exists() else set()
    any_work = False
    for msg in messages:
        ts = msg.get("ts") or ""
        if not ts or ts in handled or msg.get("bot_id") or msg.get("subtype") in (
            "channel_join",
            "channel_purpose",
        ):
            continue
        files = msg.get("files") or []
        # file_share subtype
        if not files and msg.get("subtype") == "file_share":
            files = msg.get("files") or []
        if not files:
            # skip pure text here (hotel shortlist owns text)
            handled.add(ts)
            continue
        notes = process_message(home, token, channel, msg, finance_root)
        if notes:
            any_work = True
            text = "\n".join(notes)
            slack_api(
                token,
                "chat.postMessage",
                channel=channel,
                thread_ts=ts,
                text=text,
            )
            print(text)
        handled.add(ts)
    handled_file.write_text("\n".join(sorted(handled)) + "\n", encoding="utf-8")
    if messages:
        cursor_file.write_text((messages[-1].get("ts") or "") + "\n", encoding="utf-8")
    if not any_work:
        print("no new hotel invoices")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
