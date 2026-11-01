"""
Il Lab: i progetti grossi si costruiscono da soli, a blocchi, dietro le quinte.

Ogni progetto è una cartella in `~/lab/<nome>/` con un `PIANO.md` (le cose da fare, da spuntare,
e un diario). Carlo lo avvia con `lab avvia <nome>` (bin/lab, stesso socket di manda-file); il
server lavora un blocco alla volta: un turno di Claude Code nella cartella del progetto, con una
sessione nuova ogni volta (il filo sta nel piano, non nella conversazione) e al massimo
BLOCK_MINUTES. Ogni blocco chiude con una riga "STATO: continua | finito | bloccato <motivo>".
Prima di ogni blocco legge i consumi: oltre l'85% delle 5 ore o il 90% della settimana va in
pausa fino all'azzeramento (così a Will resta margine per parlare con Carlo) e riparte da solo;
lo stesso se il turno si ferma per il limite. Will riceve messaggi brevi (partenza, pausa,
ripresa) senza turni di Carlo; a fine lavoro, a lavoro bloccato o dopo due blocchi falliti di
fila arriva invece un avviso a Carlo, che controlla e riferisce. Un progetto alla volta. Lo stato
sta in `dati/lab.json`: un riavvio del server riprende da dove era.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import chat_texts
from .claude_runner import ClaudeTurn, TurnResult
from .config import DATA_DIR
from .delivery import send_text
from .usage import LOCAL_TIME, read_usage

log = logging.getLogger(__name__)

LAB_DIR = Path.home() / "lab"
STATE_FILE = DATA_DIR / "lab.json"
MODEL = "opus"
BLOCK_MINUTES = 30
LIMITS = {"five_hour": 85, "seven_day": 90}
RESUME_MARGIN = timedelta(minutes=3)
RETRY_SECONDS = 15 * 60
MAX_FAILURES = 2
FAILURE_PAUSE_SECONDS = 60
STATUS_LINE = re.compile(r"STATO:\s*(continua|finito|bloccato)\b(.*)", re.IGNORECASE)


class Lab:
    def __init__(self, project_bot, notify_carlo: Callable[[str], None], claude_path: str,
                 state_file: Path = STATE_FILE, lab_dir: Path = LAB_DIR, reader=read_usage,
                 turn_factory=ClaudeTurn, clock=lambda: datetime.now(LOCAL_TIME)):
        self.bot, self.notify_carlo, self.claude_path = project_bot, notify_carlo, claude_path
        self.state_file, self.lab_dir, self.reader = state_file, lab_dir, reader
        self.turn_factory, self.clock = turn_factory, clock
        self._task: asyncio.Task | None = None

    def command(self, action: str, project: str = "") -> tuple[bool, str]:
        if action == "avvia":
            return self.start(project)
        if action == "ferma":
            return self.stop()
        if action == "stato":
            return True, self.describe()
        return False, f"azione sconosciuta: {action} (usa avvia, ferma o stato)"

    def start(self, project: str) -> tuple[bool, str]:
        state = self._load()
        if state.get("stato") == "attivo":
            return False, f"il Lab sta già lavorando su {state['progetto']}: prima fermalo"
        if not (self.lab_dir / project / "PIANO.md").is_file():
            return False, f"manca {self.lab_dir / project / 'PIANO.md'}: scrivi prima il piano"
        self._save({"progetto": project, "stato": "attivo", "blocchi": 0, "falliti": 0})
        self._say(chat_texts.lab_started(project))
        self._launch()
        return True, f"Lab avviato su {project}"

    def stop(self) -> tuple[bool, str]:
        state = self._load()
        if state.get("stato") != "attivo":
            return False, "il Lab non sta lavorando"
        self._finish(state, "fermato")
        if self._task:
            self._task.cancel()
        return True, f"Lab fermato su {state['progetto']} dopo {state.get('blocchi', 0)} blocchi"

    def describe(self) -> str:
        state = self._load()
        if not state:
            return "il Lab non ha mai lavorato"
        text = f"{state['progetto']}: {state['stato']}, {state.get('blocchi', 0)} blocchi fatti"
        if state.get("riprendi_alle"):
            text += f", in pausa fino alle {datetime.fromisoformat(state['riprendi_alle']):%H:%M}"
        return text

    def resume_on_start(self) -> None:
        if self._load().get("stato") == "attivo":
            log.info("Riprendo il lavoro del Lab interrotto dal riavvio")
            self._launch()

    def _launch(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while (state := self._load()).get("stato") == "attivo":
            try:
                await self._step(state)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Errore nel giro del Lab")
                await self._sleep(RETRY_SECONDS)

    async def _step(self, state: dict) -> None:
        if state.get("riprendi_alle"):
            wait = (datetime.fromisoformat(state["riprendi_alle"]) - self.clock()).total_seconds()
            if wait > 0:
                await self._sleep(wait)
                return
            state.pop("riprendi_alle")
            self._save(state)
            self._say(chat_texts.LAB_RESUMED)
        pause = await self._pause_until()
        if pause is None:
            await self._sleep(RETRY_SECONDS)
            return
        if pause:
            self._pause(state, pause)
            return
        result = await self._block(state["progetto"])
        if result.kind == "usage_limit":
            self._pause(state, await self._pause_until(force=True) or
                        self.clock() + timedelta(seconds=RETRY_SECONDS))
        elif result.kind == "ok":
            self._after_block(state, result.text)
        else:
            state["falliti"] = state.get("falliti", 0) + 1
            self._save(state)
            if state["falliti"] >= MAX_FAILURES:
                self._finish(state, "fallito", result.text)
            else:
                await self._sleep(FAILURE_PAUSE_SECONDS)

    def _after_block(self, state: dict, text: str) -> None:
        state["blocchi"] = state.get("blocchi", 0) + 1
        state["falliti"] = 0
        self._save(state)
        match = STATUS_LINE.search(text.strip().splitlines()[-1] if text.strip() else "")
        outcome = match.group(1).lower() if match else "continua"
        if outcome != "continua":
            self._finish(state, outcome, text)

    async def _pause_until(self, force: bool = False) -> datetime | bool | None:
        """Ora a cui ripartire se i consumi sono oltre i limiti, False se si può lavorare, None
        se i consumi non si leggono. Con force sceglie la finestra più piena anche sotto soglia."""
        result = await self.reader(self.bot.http)
        if not result:
            return None
        usage, _ = result
        windows = [usage.get(key) or {} for key in LIMITS]
        over = [w for key, w in zip(LIMITS, windows)
                if (w.get("utilization") or 0) >= LIMITS[key] and w.get("resets_at")]
        if force and not over:
            over = sorted((w for w in windows if w.get("resets_at")),
                          key=lambda w: -(w.get("utilization") or 0))[:1]
        if not over:
            return False
        latest = max(datetime.fromisoformat(w["resets_at"]) for w in over)
        return latest.astimezone(LOCAL_TIME) + RESUME_MARGIN

    def _pause(self, state: dict, until: datetime) -> None:
        state["riprendi_alle"] = until.isoformat()
        self._save(state)
        self._say(chat_texts.lab_paused(state["progetto"], until))

    async def _block(self, project: str):
        folder = self.lab_dir / project
        turn = self.turn_factory(
            claude_path=self.claude_path, cwd=folder, prompt=chat_texts.lab_block(folder),
            model=MODEL, session_id=None, system_prompt=chat_texts.LAB_SYSTEM,
            log_name=f"lab/{project}", on_session=lambda session: None)
        try:
            return await asyncio.wait_for(turn.run(), BLOCK_MINUTES * 60)
        except asyncio.TimeoutError:
            return TurnResult("failed", f"blocco oltre i {BLOCK_MINUTES} minuti")
        finally:
            turn.kill()

    def _finish(self, state: dict, outcome: str, text: str = "") -> None:
        state.update(stato=outcome)
        state.pop("riprendi_alle", None)
        self._save(state)
        log.info("Lab su %s: %s", state["progetto"], outcome)
        if outcome != "fermato":
            self.notify_carlo(chat_texts.lab_report(state["progetto"], outcome,
                                                    state.get("blocchi", 0), text))

    def _say(self, text: str) -> None:
        asyncio.get_running_loop().create_task(
            send_text(self.bot.api, self.bot.assistant.authorized_user, text))

    async def _sleep(self, seconds: float) -> None:
        # A tratti di 5 minuti: un cambio d'ora o un orologio spostato non allunga la pausa.
        await asyncio.sleep(min(max(seconds, 0), 300))

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        self.state_file.write_text(json.dumps(state, ensure_ascii=False))
