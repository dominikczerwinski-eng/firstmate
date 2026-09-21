#!/usr/bin/env bash
# fm-hotel-bot.sh - safe Slack hotel-intent workflow for #hotele.
# Commands: parse TEXT, route TEXT, resolve TEXT, lookup TEXT, shortlist PLACE FILE, finish INDEX PLACE, poll.
# Booking results are supplied by a human or separate read-only adapter.
set -euo pipefail

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
STATE_DIR=${FM_HOTEL_STATE_DIR:-${FM_HOME:-$ROOT}/state/hotel}
mkdir -p "$STATE_DIR"
die() { printf 'fm-hotel-bot: %s\n' "$*" >&2; exit 2; }
parser() { "$ROOT/bin/fm-hotel-parse.py" "$1"; }

route() {
  python3 -c 'import json,sys
i=json.load(sys.stdin)
route="shortlist" if i["location"] else ("lookup_pipedrive" if i["customer_query"] else "clarify")
print(json.dumps({"route":route,"intent":i}, ensure_ascii=False, sort_keys=True))' <<<"$1"
}

resolve() {
  local intent="$1"
  python3 -c 'import json,sys
i=json.load(sys.stdin)
if i["location"]:
 print(json.dumps({"route":"shortlist","location":i["location"],"intent":i}, ensure_ascii=False, sort_keys=True))
else:
 print(json.dumps({"route":"lookup_pipedrive","query":i["customer_query"],"intent":i}, ensure_ascii=False, sort_keys=True))' <<<"$intent"
}

lookup() {
  [[ -n "${PIPEDRIVE_API_TOKEN:-}" ]] || die "PIPEDRIVE_API_TOKEN nie jest ustawiony"
  QUERY="$1" python3 - <<'PY'
import json, os, sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
base = os.environ.get("PIPEDRIVE_BASE_URL", "https://api.pipedrive.com")
url = base.rstrip("/") + "/v1/organizations/search?" + urlencode({"term": os.environ["QUERY"], "api_token": os.environ["PIPEDRIVE_API_TOKEN"]})
with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=20) as response:
    payload = json.load(response)
items = (payload.get("data") or {}).get("items", [])
matches = []
for item in items:
    org = item.get("item", item)
    matches.append({"id": org.get("id"), "name": org.get("name", ""), "address": org.get("address", "")})
if len(matches) == 1:
    print(json.dumps({"route": "shortlist", "location": matches[0]["address"] or matches[0]["name"], "match": matches[0]}, ensure_ascii=False))
elif not matches:
    print(json.dumps({"route": "clarify", "reason": "no_pipedrive_match"}, ensure_ascii=False))
else:
    print(json.dumps({"route": "clarify", "reason": "multiple_pipedrive_matches", "matches": matches[:5]}, ensure_ascii=False))
PY
}

render_shortlist() {
  local place="$1" file="$2"
  [[ -r "$file" ]] || die "shortlist wymaga pliku JSON z wynikami Booking"
  PLACE="$place" python3 - "$file" <<'PY'
import json, os, sys
rows = json.load(open(sys.argv[1], encoding="utf-8"))[:3]
if not rows:
    print("Nie znalazłem hoteli spełniających kryteria. Podeślij inne miasto lub budżet.")
    raise SystemExit
print("Propozycje Booking dla *%s* (wybierz numer):" % os.environ["PLACE"])
for n, row in enumerate(rows, 1):
    print("%d. *%s* - %s PLN, ocena %s, %s km - <%s|Booking>" %
          (n, row.get("name", "Hotel bez nazwy"), row.get("price_pln", "?"),
           row.get("score", "?"), row.get("distance_km", "?"), row.get("url", "")))
print("Kryteria: śniadanie, parking (płatny OK), karta, do ok. 400 PLN, ocena min. 8, Booking, do ok. 5 km.")
PY
}

finish() {
  local index="$1" place="$2"
  [[ "$index" =~ ^[1-3]$ ]] || die "numer hotelu musi być od 1 do 3"
  # Concise captain ping only — no Awios tutorial checklist.
  local captain="${HOTEL_CAPTAIN_SLACK_ID:-U0BK3DW0ZL2}"
  local guest_name
  guest_name=$(grep -E '^HOTEL_GUEST_NAME=' "${FM_HOME:-$ROOT}/config/hotel-guest.env" 2>/dev/null | cut -d= -f2- || echo "Sebastian Waloch")
  cat <<EOF
<@${captain}> wybrany hotel nr ${index} · ${place}
Gość: ${guest_name}
(link Booking w shortliście / last-pick)
EOF
}

poll() {
  [[ -n "${SLACK_BOT_TOKEN:-}" ]] || die "SLACK_BOT_TOKEN nie jest ustawiony"
  [[ -n "${FM_HOTEL_CHANNEL_ID:-}" ]] || die "ustaw FM_HOTEL_CHANNEL_ID dla #hotele"
  local cursor_file="$STATE_DIR/slack.cursor" cursor="" response temp
  [[ -r "$cursor_file" ]] && cursor=$(<"$cursor_file")
  local url="https://slack.com/api/conversations.history?channel=${FM_HOTEL_CHANNEL_ID}&limit=50"
  [[ -n "$cursor" ]] && url="$url&oldest=$cursor"
  response=$(curl --fail --silent --show-error -H "Authorization: Bearer $SLACK_BOT_TOKEN" "$url")
  temp=$(mktemp "$STATE_DIR/slack.XXXXXX.json")
  trap 'rm -f "$temp"' RETURN
  printf '%s' "$response" >"$temp"
  HOTEL_ROOT="$ROOT" python3 - "$STATE_DIR" "$temp" <<'PY'
import json, os, pathlib, re, subprocess, sys
state, source = sys.argv[1:]
data = json.loads(pathlib.Path(source).read_text(encoding="utf-8"))
if not data.get("ok"):
    raise SystemExit("Slack API: " + str(data.get("error", "unknown error")))
parser = pathlib.Path(os.environ["HOTEL_ROOT"]) / "bin/fm-hotel-parse.py"
messages = sorted(data.get("messages", []), key=lambda x: x.get("ts", ""))
for msg in messages:
    text = (msg.get("text") or "").strip()
    if not text or msg.get("bot_id"):
        continue
    parsed = json.loads(subprocess.check_output([str(parser), text], text=True))
    route = "shortlist" if parsed["location"] else ("lookup_pipedrive" if parsed["customer_query"] else "clarify")
    record = {"ts": msg.get("ts"), "user": msg.get("user"), "route": route, "intent": parsed}
    safe = re.sub(r"[^0-9-]", "", msg.get("ts", ""))
    pathlib.Path(state, "intent-%s.json" % safe).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
if messages:
    pathlib.Path(state, "slack.cursor").write_text(messages[-1].get("ts", "") + "\n", encoding="utf-8")
PY
}

case "${1:-}" in
  parse) [[ $# -eq 2 ]] || die "usage: parse TEXT"; parser "$2" ;;
  route) [[ $# -eq 2 ]] || die "usage: route TEXT"; route "$(parser "$2")" ;;
  resolve) [[ $# -eq 2 ]] || die "usage: resolve TEXT"; resolve "$(parser "$2")" ;;
  lookup) [[ $# -eq 2 ]] || die "usage: lookup CUSTOMER"; lookup "$2" ;;
  shortlist) [[ $# -eq 3 ]] || die "usage: shortlist PLACE FILE"; render_shortlist "$2" "$3" ;;
  finish) [[ $# -eq 3 ]] || die "usage: finish INDEX PLACE"; finish "$2" "$3" ;;
  poll) [[ $# -eq 1 ]] || die "usage: poll"; poll ;;
  *) die "usage: parse|route|resolve|lookup|shortlist|finish|poll" ;;
esac
