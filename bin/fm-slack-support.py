#!/usr/bin/env python3
"""Slack support poller for the Fenedo support channel.

This module deliberately uses only Python's standard library.  It reads the
Slack bot token from the effective firstmate home's .env file, never prints it,
and keeps its cursor and message decisions in state/.slack-support/.
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
)
HIGH_RISK_PATTERNS = (
    ("legal", "legal or compliance request"),
    ("gdpr", "privacy or data-protection request"),
    ("privacy", "privacy or data-protection request"),
    ("security", "security request"),
    ("vulnerability", "security request"),
    ("password", "credential or access request"),
    ("token", "credential or access request"),
    ("permission", "access-control request"),
    ("delete", "destructive request"),
    ("delet", "destructive request"),
    ("remove all", "destructive request"),
    ("refund", "financial request"),
    ("invoice", "financial request"),
    ("payment", "financial request"),
    ("charge", "financial request"),
    ("price", "pricing or product decision"),
    ("pricing", "pricing or product decision"),
    ("offer generation", "product behavior request"),
    ("generate offer", "product behavior request"),
    ("feature", "product request"),
    ("roadmap", "product request"),
    ("should we", "judgment call"),
    ("decide", "judgment call"),
    ("urgent", "priority judgment call"),
)


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
    lowered = " ".join((text or "").lower().split())
    for needle, reason in HIGH_RISK_PATTERNS:
        if needle in lowered:
            return "needs-human", reason
    if any(re.search(r"\b" + re.escape(term) + r"\b", lowered) for term in COSMETIC_TERMS):
        return "cosmetic", "limited to a likely visual or copy change"
    return "needs-human", "not clearly a cosmetic safe fix"


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
    return "\n".join(
        [
            "Slack support report for the fenedo-os writer.",
            "",
            "Only make a safe cosmetic or copy fix; stop and hold for firstmate if the scope changes.",
            "",
            "Reporter: " + record["author"],
            "Slack thread: " + record["thread_ts"],
            "Classification: " + record["classification"],
            "Reason: " + record["reason"],
            "",
            record["text"],
        ]
    )


def route_cosmetic(home: Path, root: Path, record: dict) -> str:
    task_id = "slack-support-" + ts_key(record["ts"])
    (home / "data").mkdir(mode=0o700, parents=True, exist_ok=True)
    body_path = home / "state" / "slack-support" / (task_id + ".md")
    body_path.write_text(task_body(record) + "\n", encoding="utf-8")
    os.chmod(body_path, 0o600)
    tasks = root / "bin" / "fm-tasks-axi.sh"
    if not tasks.is_file():
        raise SupportError("the fenedo-os route helper is missing: bin/fm-tasks-axi.sh")
    result = subprocess.run(
        [
            str(tasks),
            "add",
            task_id,
            "Fenedo support: " + compact(record["text"], 100),
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
    store = SupportStore(home)
    lock = store.lock()
    try:
        state = store.load()
        client = SlackClient(token, base_url, timeout)
        channel = resolve_channel(client, channel_name)
        users = resolve_reporters(client, reporters_setting, reporter_ids)
        messages = fetch_messages(client, str(channel["id"]), str(state.get("latest_ts", "")), max_messages)
        new_messages = ordered_new_messages(messages, str(state.get("latest_ts", "")))
        state["channel_id"] = channel["id"]
        state["channel_name"] = channel.get("name", channel_name)
        state["reporters"] = sorted(users.values())
        output = []
        for record in state["messages"].values():
            if record.get("status") != "route-pending":
                continue
            try:
                record["task_id"] = route_cosmetic(home, root, record)
                record["status"] = "queued-for-fenedo-os"
                record.pop("route_error", None)
                output.append("cosmetic " + record["ts"] + " queued as " + record["task_id"] + " for fenedo-os")
            except SupportError as exc:
                record["route_error"] = str(exc)
        for message in new_messages:
            ts = str(message["ts"])
            state["latest_ts"] = max(str(state.get("latest_ts", "")), ts)
            user_id = str(message.get("user", ""))
            author = users.get(user_id)
            if not author or message.get("subtype") or message.get("bot_id"):
                continue
            text = str(message.get("text", "")).strip()
            if not text:
                continue
            classification, reason = classify(text)
            record = {
                "ts": ts,
                "thread_ts": str(message.get("thread_ts") or ts),
                "author": author,
                "text": text,
                "classification": classification,
                "reason": reason,
                "received_at": utc_now(),
                "status": "held" if classification == "needs-human" else "routing",
                "announced": False,
            }
            if classification == "cosmetic":
                try:
                    record["task_id"] = route_cosmetic(home, root, record)
                    record["status"] = "queued-for-fenedo-os"
                    output.append("cosmetic " + ts + " queued as " + record["task_id"] + " for fenedo-os")
                except SupportError as exc:
                    record["status"] = "route-pending"
                    record["route_error"] = str(exc)
                    output.append("cosmetic " + ts + " held: " + str(exc))
            else:
                output.append("needs-human " + ts + ": " + reason)
            state["messages"][ts] = record

        for ts, record in sorted(state["messages"].items()):
            if record.get("announced"):
                continue
            payload = "Slack support " + record["status"] + " from " + record["author"] + ": " + compact(record["text"])
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
        client = SlackClient(token, config_value("SLACK_API_URL", home, "https://slack.com/api"), safe_int(config_value("SLACK_SUPPORT_TIMEOUT", home, "20"), 20, 5, 60))
        text = args.text or "Fixed in fenedo-os."
        result = client.call("chat.postMessage", payload={"channel": state["channel_id"], "thread_ts": record["thread_ts"], "text": text})
        record["status"] = "completed"
        record["reply_ts"] = str(result.get("ts", ""))
        record["completed_at"] = utc_now()
        store.save(state)
        print("replied in Slack thread " + record["thread_ts"])
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
