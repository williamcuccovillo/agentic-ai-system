#!/usr/bin/env bash
# Applica un aggiornamento del server: lancia i test, salva le modifiche come nuova versione con
# la descrizione data e riavvia il servizio. Se i test falliscono si ferma prima del riavvio.
# Uso: strumenti/aggiorna.sh "cosa è cambiato, in una frase"
# Il riavvio interrompe anche il turno di chi lo lancia: il server lo riprenderà con un avviso.
set -euo pipefail
PROGETTO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROGETTO"
DESCRIZIONE="${1:?Serve una descrizione: strumenti/aggiorna.sh \"cosa è cambiato\"}"
if ! ESITO_TEST="$(.venv/bin/python -m unittest -q 2>&1)"; then
    echo "$ESITO_TEST" | tail -30
    echo "Test falliti: niente riavvio."
    exit 1
fi
.venv/bin/pip install -q -r requirements.txt
git add -A
git commit -q -m "$DESCRIZIONE" || echo "Nessuna modifica nuova da salvare."
# Azzera il contatore dei riavvii di systemd: i riavvii voluti non devono sembrare un guasto.
systemctl --user reset-failed carlo.service || true
echo "Versione salvata, riavvio in corso."
systemctl --user restart carlo.service
