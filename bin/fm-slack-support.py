#!/usr/bin/env python3
"""Slack support poller for Fenedo (#support + optional DMs).

Polish-first replies. Reads the bot token from the effective firstmate home's
.env, never prints it, and keeps cursor/decisions in state/.slack-support/.
DMs need Slack scopes im:history + im:read (and chat:write, already required).
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = "fm-slack-support-v1"
DEFAULT_CHANNEL = "support"
DEFAULT_REPORTERS = ("all",)
COSMETIC_TERMS = (
    "alignment", "aligned", "button", "colour", "color", "copy", "css",
    "display", "font", "format", "icon", "label", "layout", "padding",
    "spacing", "text", "typo", "visual", "wording", "ui", "ux",
    # Polish cosmetic cues
    "przycisk", "kolor", "czcionka", "etykieta", "układ", "uklad", "odstęp",
    "odstep", "literówka", "literowka", "wygląd", "wyglad", "wizual",
    "interfejs", "margines", "padding", "spacing",
)
# Only these hold for firstmate/captain gate. Real bugs go to fenedo-os.
HARD_HOLD_PATTERNS = (
    ("legal", "temat prawny / compliance"),
    ("gdpr", "prywatność / ochrona danych"),
    ("rodo", "prywatność / ochrona danych"),
    ("privacy", "prywatność / ochrona danych"),
    ("prywatn", "prywatność / ochrona danych"),
    ("security", "incydent bezpieczeństwa"),
    ("bezpieczenstw", "incydent bezpieczeństwa"),
    ("vulnerability", "incydent bezpieczeństwa"),
    ("password", "sekret / dane logowania"),
    ("hasło", "sekret / dane logowania"),
    ("haslo", "sekret / dane logowania"),
    ("api key", "sekret / dane logowania"),
    ("token xox", "sekret / dane logowania"),
    ("delete all", "żądanie destrukcyjne"),
    ("usuń wszystko", "żądanie destrukcyjne"),
    ("usun wszystko", "żądanie destrukcyjne"),
    ("drop table", "żądanie destrukcyjne"),
    ("refund", "zwrot / płatność — bramka właściciela"),
    ("charge customer", "płatność klienta — bramka właściciela"),
    ("zmień cen", "zmiana cennika — bramka właściciela"),
    ("change price", "zmiana cennika — bramka właściciela"),
    ("write to pipedrive", "zapis do Pipedrive — bramka właściciela"),
    ("zapisz w pipedrive", "zapis do Pipedrive — bramka właściciela"),
    ("pipedrive write", "zapis do Pipedrive — bramka właściciela"),
    ("wyślij do klienta", "wiadomość do klienta — bramka właściciela"),
    ("wyslij do klienta", "wiadomość do klienta — bramka właściciela"),
    ("wyślij mail", "wiadomość do klienta — bramka właściciela"),
    ("wyslij mail", "wiadomość do klienta — bramka właściciela"),
    ("do klienta", "wiadomość do klienta — bramka właściciela"),
    ("send to customer", "wiadomość do klienta — bramka właściciela"),
    ("email the customer", "wiadomość do klienta — bramka właściciela"),
)

# Cues that this is a real operational bug (route to fenedo-os).
OPERATIONAL_BUG_CUES = (
    "nie działa", "nie dziala", "error", "błąd", "blad", "exception", "traceback",
    "500", "404", "timeout", "cras", "wisi", "zawies", "nie generu", "generuje",
    "oferta", "offer", "measurement", "pomiar", "karta pomiar", "pipedrive",
    "sync", "synchron", "integrac", "połączen", "polaczen", "connection",
    "login", "logowan", "nie mogę", "nie moge", "broken", "fail", "stack",
    "console", "screenshot", "zrzut",
)

# User-facing Polish templates (no LLM).
REPLY_ACK_COSMETIC = (
    "Przyjąłem. Oddaję do kolejki naprawy Fenedo OS (kosmetyka/UI). "
    "Odpiszę w tym wątku, gdy będzie gotowe."
)
REPLY_ACK_OPERATIONAL = (
    "Przyjąłem. Oddaję do kolejki naprawy Fenedo OS (diagnoza/fix). "
    "Odpiszę w tym wątku, gdy będzie aktualizacja."
)
REPLY_ACK_HELD = (
    "Przyjąłem. Ten temat trafia do kolejki poprawy z ręcznym przeglądem "
    "(bezpieczeństwo / sekrety / destrukcja / płatności / zapis zewnętrzny / decyzja). "
    "Nie lecę z tym automatycznie jako zwykły fix."
)
REPLY_COMPLETE_DEFAULT = "Naprawione po stronie Fenedo OS."


class SupportError(Exception):
    """A safe-to-display integration error that contains no credentials."""


def env_file_value(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    value = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line.startswith(key + "="):
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
    return value


def config_value(name: str, home: Path, default: str = "") -> str:
    if name in os.environ:
        return os.environ.get(name, "")
    return env_file_value(home / ".env", name) or default


def safe_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compact(text: str, limit: int = 180) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ts_key(ts: str) -> str:
    return hashlib.sha256(ts.encode("utf-8")).hexdigest()[:16]


def classify(text: str):
    """Return (classification, reason).

    operational  -> queue fenedo-os for diagnosis/fix (real support traffic)
    cosmetic     -> queue fenedo-os (UI/copy only; rare but cheap)
    needs-human  -> hold for firstmate (destructive, secrets, money write, legal)
    """
    lowered = " ".join((text or "").lower().split())
    # Hard gates: never auto-fix without human gate.
    for needle, reason in HARD_HOLD_PATTERNS:
        if needle in lowered:
            return "needs-human", reason
    # Explicit cosmetic-only cues (optional fast path).
    if any(re.search(r"\b" + re.escape(term) + r"\b", lowered) for term in COSMETIC_TERMS):
        # If it also looks like a broken feature, prefer operational.
        if any(k in lowered for k in OPERATIONAL_BUG_CUES):
            return "operational", "zgłoszenie błędu / nie działa (kolejka Fenedo OS)"
        return "cosmetic", "kosmetyka / UI / copy"
    # Default real support: operational bug queue, not captain.
    if any(k in lowered for k in OPERATIONAL_BUG_CUES) or len(lowered) >= 12:
        return "operational", "zgłoszenie operacyjne / błąd (kolejka Fenedo OS)"
    return "operational", "zgłoszenie supportowe (kolejka Fenedo OS)"



class SlackClient:
    def __init__(self, token: str, base_url: str, timeout: int):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, params=None, payload=None):
        if payload is None:
            query = urllib.parse.urlencode(params or {})
            url = self.base_url + "/" + method + ("?" + query if query else "")
            request = urllib.request.Request(url, method="GET")
        else:
            url = self.base_url + "/" + method
            body = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(url, data=body, method="POST")
            request.add_header("Content-Type", "application/json; charset=utf-8")
        request.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise SupportError("Slack API HTTP error " + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            detail = getattr(exc, "reason", None) or exc.__class__.__name__
            raise SupportError("Slack API connection failed: " + str(detail)) from None
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SupportError("Slack API returned invalid JSON") from None
        if not isinstance(result, dict) or not result.get("ok"):
            error = result.get("error", "unknown error") if isinstance(result, dict) else "invalid response"
            raise SupportError("Slack API rejected the request: " + str(error))
        return result

    def paged(self, method: str, key: str, params=None):
        params = dict(params or {})
        rows = []
        cursor = ""
        while True:
            query = dict(params)
            query["limit"] = query.get("limit", 200)
            if cursor:
                query["cursor"] = cursor
            result = self.call(method, query)
            rows.extend(result.get(key, []))
            cursor = (((result.get("response_metadata") or {}).get("next_cursor")) or "").strip()
            if not cursor:
                return rows


class SupportStore:
    def __init__(self, home: Path):
        self.root = home / "state" / "slack-support"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        self.lock_path = self.root / "poll.lock"

    def load(self):
        if not self.path.exists():
            return {"schema": SCHEMA, "latest_ts": "", "messages": {}}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise SupportError("state/slack-support/state.json is unreadable") from None
        if state.get("schema") != SCHEMA or not isinstance(state.get("messages"), dict):
            raise SupportError("state/slack-support/state.json has an unsupported schema")
        return state

    def save(self, state):
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
            os.replace(name, self.path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def lock(self):
        handle = self.lock_path.open("a+")
        os.chmod(self.lock_path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise SupportError("another Slack support poll is already running") from None
        return handle


def reporter_name(user: dict) -> str:
    profile = user.get("profile") or {}
    return str(
        user.get("real_name")
        or profile.get("real_name")
        or profile.get("display_name")
        or user.get("name")
        or ""
    )


def reporter_matches(user: dict, wanted: set) -> bool:
    profile = user.get("profile") or {}
    fields = (
        user.get("name", ""),
        user.get("real_name", ""),
        profile.get("display_name", ""),
        profile.get("real_name", ""),
    )
    for field in fields:
        normalized = str(field).casefold().strip()
        if normalized in wanted:
            return True
        first_word = re.split(r"[.\s_-]+", normalized, maxsplit=1)[0]
        if first_word in wanted:
            return True
    return False


def resolve_channel(client: SlackClient, requested: str):
    for channel in client.paged(
        "conversations.list",
        "channels",
        {"types": "public_channel", "exclude_archived": "true"},
    ):
        if channel.get("name", "").casefold() == requested.casefold():
            return channel
    try:
        private_channels = client.paged(
            "conversations.list",
            "channels",
            {"types": "private_channel", "exclude_archived": "true"},
        )
    except SupportError as exc:
        if "missing_scope" in str(exc):
            raise SupportError(
                "Slack support channel not found as public #" + requested + "; "
                "private-channel lookup needs groups:read"
            ) from None
        raise
    for channel in private_channels:
        if channel.get("name", "").casefold() == requested.casefold():
            return channel
    raise SupportError("Slack support channel not found: #" + requested)


def resolve_reporters(client: SlackClient, configured: str, explicit_ids: str):
    ids = {part.strip() for part in explicit_ids.split(",") if part.strip()}
    wanted_raw = {part.strip() for part in configured.split(",") if part.strip()}
    wanted = {part.casefold() for part in wanted_raw}
    accept_all = (not ids) and (not wanted or wanted == {"all"} or wanted == {"*"})
    users = {}
    try:
        members = client.paged("users.list", "members", {})
    except SupportError as exc:
        if "missing_scope" in str(exc):
            raise SupportError(
                "Slack app needs users:read to resolve reporters; "
                "grant that scope or set SLACK_SUPPORT_REPORTER_IDS"
            ) from None
        raise
    for user in members:
        if user.get("deleted") or user.get("is_bot") or user.get("id") == "USLACKBOT":
            continue
        user_id = str(user.get("id", ""))
        if not user_id:
            continue
        name = reporter_name(user) or str(user.get("name", "")) or ("user:" + user_id)
        if ids:
            if user_id in ids:
                users[user_id] = name
            continue
        if accept_all or reporter_matches(user, wanted):
            users[user_id] = name
    if ids and not users:
        # IDs configured but users.list could not name them: keep ID fallbacks.
        return {user_id: "user:" + user_id for user_id in ids}
    return users


def fetch_messages(client: SlackClient, channel_id: str, latest_ts: str, limit: int):
    params = {"channel": channel_id, "limit": limit}
    if latest_ts:
        params["oldest"] = latest_ts
        params["inclusive"] = "false"
    return client.paged("conversations.history", "messages", params)


def list_im_channels(client: SlackClient):
    """Return DM conversations the bot is in. Raises SupportError on missing_scope."""
    try:
        return client.paged("conversations.list", "channels", {"types": "im", "exclude_archived": "true"})
    except SupportError as exc:
        if "missing_scope" in str(exc) or "not_allowed_token_type" in str(exc):
            raise SupportError(
                "DM support needs Slack scopes im:history and im:read "
                "(reinstall the Fenek app after adding them); channel poll still works"
            ) from None
        raise


def post_user_reply(client: SlackClient, channel_id: str, thread_ts: str, text: str, source: str):
    payload = {"channel": channel_id, "text": text}
    # Channel reports stay threaded; DMs reply in the DM conversation.
    if source != "im" and thread_ts:
        payload["thread_ts"] = thread_ts
    return client.call("chat.postMessage", payload=payload)


def ack_text(classification: str) -> str:
    if classification == "cosmetic":
        return REPLY_ACK_COSMETIC
    if classification == "operational":
        return REPLY_ACK_OPERATIONAL
    return REPLY_ACK_HELD


def announce(home: Path, root: Path, key: str, payload: str):
    helper = """
set -eu
home=$1
root=$2
state=$3
key=$4
payload=$5
FM_HOME=$home FM_ROOT_OVERRIDE=$root FM_STATE_OVERRIDE=$state . "$root/bin/fm-wake-lib.sh"
fm_wake_append check "$key" "$payload"
"""
    result = subprocess.run(
        ["bash", "-c", helper, "fm-slack-support", str(home), str(root), str(home / "state"), key, payload],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SupportError("could not publish the firstmate wake")


def task_body(record: dict) -> str:
    source = record.get("source", "channel")
    return "\n".join(
        [
            "Slack support report for the fenedo-os writer.",
            "",
            "Default: diagnose and fix the reported operational bug in fenedo-os.",
            "Safe, reversible product fixes are in scope. Stop and hold for firstmate if the work needs",
            "production writes, Pipedrive writes, customer messages, payments, secrets, or legal calls.",
            "",
            "Reporter: " + record["author"],
            "Source: " + source + (" (private DM)" if source == "im" else " (channel)"),
            "Slack channel_id: " + str(record.get("channel_id", "")),
            "Slack thread: " + record["thread_ts"],
            "Classification: " + record["classification"],
            "Reason: " + record["reason"],
            "",
            record["text"],
        ]
    )


def route_to_fenedo(home: Path, root: Path, record: dict) -> str:
    task_id = "slack-support-" + ts_key(record["ts"])
    (home / "data").mkdir(mode=0o700, parents=True, exist_ok=True)
    body_path = home / "state" / "slack-support" / (task_id + ".md")
    body_path.write_text(task_body(record) + "\n", encoding="utf-8")
    os.chmod(body_path, 0o600)
    tasks = root / "bin" / "fm-tasks-axi.sh"
    if not tasks.is_file():
        raise SupportError("the fenedo-os route helper is missing: bin/fm-tasks-axi.sh")
    kind = record.get("classification") or "operational"
    prefix = "Fenedo support bug: " if kind == "operational" else "Fenedo support UI: "
    result = subprocess.run(
        [
            str(tasks),
            "add",
            task_id,
            prefix + compact(record["text"], 100),
            "--kind",
            "ship",
            "--repo",
            "fenedo-os",
            "--body-file",
            str(body_path),
            "--queue",
        ],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env=dict(os.environ, FM_HOME=str(home), FM_DATA_OVERRIDE="", FM_STATE_OVERRIDE=""),
    )
    if result.returncode != 0:
        raise SupportError("could not queue the fenedo-os backlog item")
    return task_id


def ordered_new_messages(messages, latest_ts: str):
    result = []
    for message in messages:
        ts = str(message.get("ts", ""))
        if not ts or (latest_ts and ts <= latest_ts):
            continue
        result.append(message)
    return sorted(result, key=lambda item: str(item.get("ts", "")))


def ingest_messages(
    *,
    home: Path,
    root: Path,
    client: SlackClient,
    state: dict,
    users: dict,
    messages,
    channel_id: str,
    channel_label: str,
    source: str,
    cursor_key: str,
    output: list,
):
    """Ingest new messages from one conversation into durable state."""
    latest = str(state.get(cursor_key, "") or "")
    if source == "im":
        im_cursors = state.setdefault("im_cursors", {})
        if not isinstance(im_cursors, dict):
            im_cursors = {}
            state["im_cursors"] = im_cursors
        latest = str(im_cursors.get(channel_id, "") or "")
    new_messages = ordered_new_messages(messages, latest)
    max_ts = latest
    for message in new_messages:
        ts = str(message["ts"])
        max_ts = max(max_ts, ts)
        user_id = str(message.get("user", ""))
        author = users.get(user_id)
        if not author or message.get("subtype") or message.get("bot_id"):
            continue
        # Ignore the bot's own messages and bare empties.
        text = str(message.get("text", "")).strip()
        if not text:
            continue
        classification, reason = classify(text)
        thread_ts = str(message.get("thread_ts") or ts)
        record = {
            "ts": ts,
            "thread_ts": thread_ts,
            "channel_id": channel_id,
            "channel_label": channel_label,
            "source": source,
            "author": author,
            "author_id": user_id,
            "text": text,
            "classification": classification,
            "reason": reason,
            "received_at": utc_now(),
            "status": "held" if classification == "needs-human" else "routing",
            "announced": False,
            "acked": False,
        }
        # Polish user ack in-thread / in-DM (best-effort; never blocks routing).
        try:
            ack = post_user_reply(client, channel_id, thread_ts, ack_text(classification), source)
            record["ack_ts"] = str(ack.get("ts", ""))
            record["acked"] = True
        except SupportError as exc:
            record["ack_error"] = str(exc)
        if classification in ("cosmetic", "operational"):
            try:
                record["task_id"] = route_to_fenedo(home, root, record)
                record["status"] = "queued-for-fenedo-os"
                output.append(
                    classification
                    + " "
                    + ts
                    + " ("
                    + source
                    + ") queued as "
                    + record["task_id"]
                    + " for fenedo-os"
                )
            except SupportError as exc:
                record["status"] = "route-pending"
                record["route_error"] = str(exc)
                output.append(classification + " " + ts + " held: " + str(exc))
        else:
            output.append("needs-human " + ts + " (" + source + "): " + reason)
        state["messages"][ts] = record
    if source == "im":
        if max_ts:
            state.setdefault("im_cursors", {})[channel_id] = max_ts
    else:
        if max_ts:
            state[cursor_key] = max_ts


def poll(args):
    home = Path(args.home).resolve()
    root = Path(args.root).resolve()
    token = config_value("SLACK_BOT_TOKEN", home)
    if not token:
        raise SupportError("missing SLACK_BOT_TOKEN in " + str(home / ".env") + "; Slack support is disabled")
    channel_name = config_value("SLACK_SUPPORT_CHANNEL", home, DEFAULT_CHANNEL)
    reporters_setting = config_value("SLACK_SUPPORT_REPORTERS", home, ",".join(DEFAULT_REPORTERS))
    reporter_ids = config_value("SLACK_SUPPORT_REPORTER_IDS", home)
    timeout = safe_int(config_value("SLACK_SUPPORT_TIMEOUT", home, "20"), 20, 5, 60)
    max_messages = safe_int(config_value("SLACK_SUPPORT_MAX_MESSAGES", home, "25"), 25, 1, 100)
    base_url = config_value("SLACK_API_URL", home, "https://slack.com/api")
    dm_enabled_cfg = config_value("SLACK_SUPPORT_DM", home, "on").strip().casefold()
    want_dm = dm_enabled_cfg not in ("0", "off", "false", "no")
    store = SupportStore(home)
    lock = store.lock()
    try:
        state = store.load()
        client = SlackClient(token, base_url, timeout)
        channel = resolve_channel(client, channel_name)
        users = resolve_reporters(client, reporters_setting, reporter_ids)
        output = []
        # Retry route-pending cosmetics first.
        for record in state["messages"].values():
            if record.get("status") != "route-pending":
                continue
            try:
                record["task_id"] = route_to_fenedo(home, root, record)
                record["status"] = "queued-for-fenedo-os"
                record.pop("route_error", None)
                output.append(
                    "cosmetic " + record["ts"] + " queued as " + record["task_id"] + " for fenedo-os"
                )
            except SupportError as exc:
                record["route_error"] = str(exc)
        # Public support channel.
        channel_id = str(channel["id"])
        channel_messages = fetch_messages(
            client, channel_id, str(state.get("latest_ts", "")), max_messages
        )
        state["channel_id"] = channel_id
        state["channel_name"] = channel.get("name", channel_name)
        state["reporters"] = sorted(users.values())
        ingest_messages(
            home=home,
            root=root,
            client=client,
            state=state,
            users=users,
            messages=channel_messages,
            channel_id=channel_id,
            channel_label="#" + str(channel.get("name", channel_name)),
            source="channel",
            cursor_key="latest_ts",
            output=output,
        )
        # Private DMs (optional; requires im:* scopes).
        state["dm_enabled"] = False
        state.pop("dm_error", None)
        if want_dm:
            try:
                ims = list_im_channels(client)
                state["dm_enabled"] = True
                state.pop("dm_error", None)
                skipped = 0
                for im in ims:
                    im_id = str(im.get("id", ""))
                    if not im_id:
                        continue
                    try:
                        im_messages = fetch_messages(
                            client,
                            im_id,
                            str((state.get("im_cursors") or {}).get(im_id, "")),
                            max_messages,
                        )
                    except SupportError as exc:
                        # Closed/stale DMs often return channel_not_found; skip one, keep others.
                        err = str(exc)
                        if "channel_not_found" in err or "invalid_channel" in err:
                            skipped += 1
                            continue
                        raise
                    ingest_messages(
                        home=home,
                        root=root,
                        client=client,
                        state=state,
                        users=users,
                        messages=im_messages,
                        channel_id=im_id,
                        channel_label="dm:" + im_id,
                        source="im",
                        cursor_key="latest_ts",
                        output=output,
                    )
                if skipped:
                    # Local diagnostic only; do not wake firstmate every poll.
                    pass
            except SupportError as exc:
                state["dm_enabled"] = False
                state["dm_error"] = str(exc)
                # Only surface hard DM disable (missing scopes), not per-IM skips.
                output.append("dm-disabled: " + str(exc))
        # Firstmate wakes for unannounced records.
        for ts, record in sorted(state["messages"].items()):
            if record.get("announced"):
                continue
            src = record.get("source", "channel")
            payload = (
                "Slack support "
                + record["status"]
                + " ("
                + src
                + ") from "
                + record["author"]
                + ": "
                + compact(record["text"])
            )
            try:
                announce(home, root, "slack-support:" + ts, payload)
                record["announced"] = True
            except SupportError as exc:
                record["announce_error"] = str(exc)
                output.append("wake pending for " + ts + ": " + str(exc))
        store.save(state)
        if not output:
            print("no new Slack support reports")
        else:
            for line in output:
                print(line)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    return 0


def find_record(state, ts: str):
    record = state.get("messages", {}).get(ts)
    if not record:
        raise SupportError("no Slack support record for message " + ts)
    return record


def complete(args):
    home = Path(args.home).resolve()
    token = config_value("SLACK_BOT_TOKEN", home)
    if not token:
        raise SupportError("missing SLACK_BOT_TOKEN in " + str(home / ".env") + "; Slack support is disabled")
    store = SupportStore(home)
    lock = store.lock()
    try:
        state = store.load()
        record = find_record(state, args.ts)
        client = SlackClient(
            token,
            config_value("SLACK_API_URL", home, "https://slack.com/api"),
            safe_int(config_value("SLACK_SUPPORT_TIMEOUT", home, "20"), 20, 5, 60),
        )
        text = args.text or REPLY_COMPLETE_DEFAULT
        channel_id = str(record.get("channel_id") or state.get("channel_id") or "")
        if not channel_id:
            raise SupportError("support record has no channel_id; cannot reply")
        source = str(record.get("source") or "channel")
        result = post_user_reply(client, channel_id, str(record.get("thread_ts") or record["ts"]), text, source)
        record["status"] = "completed"
        record["reply_ts"] = str(result.get("ts", ""))
        record["completed_at"] = utc_now()
        store.save(state)
        where = "DM" if source == "im" else "wątku Slack"
        print("odpowiedziano w " + where + " " + str(record.get("thread_ts") or record["ts"]))
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    return 0


def status(args):
    home = Path(args.home).resolve()
    state = SupportStore(home).load()
    counts = {}
    for record in state.get("messages", {}).values():
        key = record.get("status", "unknown")
        counts[key] = counts.get(key, 0) + 1
    print("channel: #" + config_value("SLACK_SUPPORT_CHANNEL", home, DEFAULT_CHANNEL))
    print("token: configured" if config_value("SLACK_BOT_TOKEN", home) else "token: missing")
    print("language: pl")
    dm_cfg = config_value("SLACK_SUPPORT_DM", home, "on").strip().casefold()
    print("dm_config: " + ("on" if dm_cfg not in ("0", "off", "false", "no") else "off"))
    if "dm_enabled" in state:
        print("dm_enabled: " + ("yes" if state.get("dm_enabled") else "no"))
    if state.get("dm_error"):
        print("dm_error: " + str(state.get("dm_error")))
    print("latest: " + str(state.get("latest_ts", "") or "(none)"))
    for key in sorted(counts):
        print(key + ": " + str(counts[key]))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Poll and route Fenedo Slack support reports.")
    parser.add_argument("--home", default=os.environ.get("FM_HOME") or str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--root", default=os.environ.get("FM_ROOT_OVERRIDE") or str(Path(__file__).resolve().parent.parent))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("poll", help="poll new support messages")
    sub.add_parser("status", help="show local support state without network")
    complete_parser = sub.add_parser("complete", help="reply in the report's Slack thread after the fix lands")
    complete_parser.add_argument("ts", help="Slack message timestamp")
    complete_parser.add_argument("text", nargs="?", help="short completion reply")
    args = parser.parse_args()
    command = args.command or "poll"
    try:
        if command == "poll":
            return poll(args)
        if command == "complete":
            return complete(args)
        if command == "status":
            return status(args)
        parser.error("unknown command: " + command)
    except SupportError as exc:
        print("fm-slack-support: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
