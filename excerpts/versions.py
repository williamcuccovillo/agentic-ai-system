"""
Versioni del codice del server e rete di sicurezza per gli aggiornamenti.

Il progetto è un archivio git. All'avvio il server salva come versione qualunque modifica non
ancora salvata (così ciò che sta girando è sempre una versione recuperabile) e se ne ricorda.
Dopo un po' di funzionamento regolare marca quella versione con l'etichetta `ultima-buona`.
Se un aggiornamento impedisce al server di ripartire, systemd lancia `strumenti/ripristina.sh`,
che torna a `ultima-buona`, mette da parte la versione rotta in un ramo `rotta-<data>` e lascia
una nota in `dati/ripristino.json`. Alla ripartenza il server legge la nota (qui) e la passa a
Carlo, che spiega a Will cosa è successo.
"""

import json
import logging
import subprocess

from .config import DATA_DIR, PROJECT_DIR

log = logging.getLogger(__name__)

GOOD_TAG = "ultima-buona"
ROLLBACK_NOTE = DATA_DIR / "ripristino.json"


def _git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(PROJECT_DIR), *args],
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def snapshot_running_version() -> str | None:
    """Salva le modifiche non salvate e restituisce l'identificativo della versione in uso."""
    if not (PROJECT_DIR / ".git").is_dir():
        log.warning("Il progetto non è un archivio git: niente versioni né ripristino")
        return None
    try:
        if _git("status", "--porcelain"):
            _git("add", "-A")
            _git("commit", "-q", "-m", "Avvio del server con modifiche non salvate")
            log.info("Modifiche non salvate registrate come nuova versione")
        return _git("rev-parse", "HEAD")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        log.error("Impossibile salvare la versione in uso: %s", error)
        return None


def mark_good(commit: str) -> None:
    try:
        _git("tag", "-f", GOOD_TAG, commit)
        log.info("Versione %s marcata come ultima buona", commit[:10])
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        log.error("Impossibile marcare la versione buona: %s", error)


def take_rollback_note() -> dict | None:
    """Legge (e cancella) la nota lasciata da ripristina.sh, se c'è."""
    if not ROLLBACK_NOTE.is_file():
        return None
    try:
        note = json.loads(ROLLBACK_NOTE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        note = {"errore_lettura": str(error)}
    ROLLBACK_NOTE.unlink(missing_ok=True)
    return note
