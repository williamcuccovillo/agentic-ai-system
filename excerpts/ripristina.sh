#!/usr/bin/env bash
# Rete di sicurezza, lanciata da systemd (carlo-ripristino.service) ogni volta che il servizio
# carlo si ferma per un errore. Se la versione in uso non è "ultima-buona", la mette da parte in un
# ramo git "rotta-<data>", torna a "ultima-buona" e riavvia subito il servizio. Se invece stava già
# girando la versione buona non c'è niente da ripristinare: i nuovi tentativi li fa systemd da
# solo, fino al suo limite (3 avvii in 5 minuti), così un guasto persistente non gira all'infinito.
# In entrambi i casi lascia una nota in dati/ripristino.json, che il server passa a Carlo.
set -uo pipefail
PROGETTO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROGETTO" || exit 1
ORA="$(date +%Y%m%d-%H%M%S)"
ERRORI="$(journalctl --user -u carlo.service -n 30 --no-pager -o cat 2>/dev/null | tail -c 3000)"

git add -A && git commit -q -m "Versione che non ripartiva ($ORA)" >/dev/null 2>&1
ROTTA="$(git rev-parse --short HEAD 2>/dev/null)"
DESCRIZIONE="$(git log -1 --format=%s 2>/dev/null)"
BUONA="$(git rev-parse -q --verify --short ultima-buona 2>/dev/null)"
RAMO=""
RIPRISTINATA=""
if [ -n "$BUONA" ] && [ "$(git rev-parse HEAD)" != "$(git rev-parse ultima-buona)" ]; then
    RAMO="rotta-$ORA"
    git branch "$RAMO" HEAD
    if ! git diff --quiet HEAD ultima-buona -- requirements.txt; then
        CAMBIO_LIBRERIE=1
    fi
    git reset -q --hard ultima-buona
    RIPRISTINATA="$BUONA"
    [ -n "${CAMBIO_LIBRERIE:-}" ] && "$PROGETTO/.venv/bin/pip" install -q -r requirements.txt
fi

mkdir -p dati
ROTTA="$ROTTA" DESCRIZIONE="$DESCRIZIONE" RAMO="$RAMO" RIPRISTINATA="$RIPRISTINATA" ERRORI="$ERRORI" \
python3 -I -c '
import json, os, sys
e = os.environ
nota = {"versione_rotta": e["ROTTA"], "descrizione": e["DESCRIZIONE"], "ramo": e["RAMO"],
        "versione_ripristinata": e["RIPRISTINATA"], "errori": e["ERRORI"]}
json.dump(nota, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
' dati/ripristino.json

if [ -n "$RIPRISTINATA" ]; then
    systemctl --user reset-failed carlo.service
    systemctl --user start carlo.service
fi
