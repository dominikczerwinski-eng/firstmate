#!/usr/bin/env bash
# Hotel bot parser, routing, shortlist, and finish contract.
set -euo pipefail
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

json=$("$ROOT"/bin/fm-hotel-bot.sh parse 'Hotel w Krakowie 12-14/10, 2 osoby, śniadanie, parking, budżet 450 PLN, karta')
python3 -c 'import json,sys
i=json.loads(sys.argv[1])
assert i["location"] == "Krakowie"
assert i["guests"] == 2 and i["budget_pln"] == 450
assert i["breakfast"] and i["parking"] and i["payment"] == "card"' "$json"

route=$("$ROOT"/bin/fm-hotel-bot.sh route 'Nocleg dla klient: Acme Sp. z o.o., 2 noce')
python3 -c 'import json,sys; assert json.loads(sys.argv[1])["route"] == "lookup_pipedrive"' "$route"

cat > "$TMP/results.json" <<'EOF'
[{"name":"Hotel Testowy","price_pln":399,"score":8.4,"distance_km":2.1,"url":"https://www.booking.com/hotel/test"}]
EOF
out=$("$ROOT"/bin/fm-hotel-bot.sh shortlist Kraków "$TMP/results.json")
grep -q 'Hotel Testowy' <<<"$out"
grep -q 'Kryteria:' <<<"$out"
finish=$("$ROOT"/bin/fm-hotel-bot.sh finish 1 Kraków)
grep -q 'Awios' <<<"$finish"
grep -q 'nie płaci automatycznie' <<<"$finish"
echo 'ok: hotel bot parser and routing'
