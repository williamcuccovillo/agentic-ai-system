"""
Gli agenti della squadra di Carlo: lavorano su incarico, senza una chat loro.

Ogni agente ha una cartella in `~/assistants/<nome>/` con `agente.toml` (nome, modello, minuti
massimi, spazio di lavoro, file di istruzioni) e le istruzioni, che l'agente legge ma non può
modificare. Carlo assegna un incarico con il comando `incarica` (bin/, stesso socket di
`manda-file`): il server avvia un turno di Claude Code nello spazio di lavoro dell'agente e,
quando finisce, gira la risposta a Carlo come avviso del server; Carlo la riferisce a Will.
La conversazione dell'agente continua da un incarico all'altro (sessione salvata), un incarico
alla volta per agente, nell'ordine in cui arrivano. Un incarico interrotto da un riavvio riparte
da solo all'accensione.
Confinamento (regole di Claude Code passate al turno, non le impostazioni dell'utente, che
permettono tutto): scrive solo nel suo spazio, legge la sua cartella, usa il web, non legge il
server né le chiavi; il Lab lo leggono ma non lo modificano (le modifiche le chiedono a Carlo,
vedi ~/lab/LAB.md). I comandi da terminale per ora sono spenti: la sandbox che li confinerebbe
non parte su questa VPS (vedi DECISIONI.md).
"""

import asyncio
import json
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import chat_texts
from .claude_runner import ClaudeTurn
from .lab import LAB_DIR

log = logging.getLogger(__name__)

AGENTS_DIR = Path.home() / "assistants"
STORAGE_CHAT = 0  # gli agenti non hanno chat: sessione e incarico in corso si salvano qui
SECRET_FOLDERS = ("carlo", ".claude", ".ssh", ".config", "prove")


@dataclass
class Agent:
    key: str
    name: str
    folder: Path
    workspace: Path
    model: str
    max_minutes: float
    instructions: str


def discover_agents(base: Path = AGENTS_DIR) -> dict[str, Agent]:
    agents = {}
    for config in sorted(base.glob("*/agente.toml")):
        folder = config.parent
        try:
            raw = tomllib.loads(config.read_text())
            text = "\n\n".join((folder / name).read_text() for name in raw["istruzioni"])
            agents[folder.name] = Agent(folder.name, raw["nome"], folder, folder / raw["spazio"],
                                        raw["modello"], float(raw["minuti_massimi"]), text)
        except (OSError, KeyError, ValueError) as error:
            log.error("Agente in %s non caricato: %s", folder, error)
    return agents


def permission_args(agent: Agent) -> list[str]:
    home = Path.home()
    rules = {"permissions": {
        "allow": ["WebSearch", "WebFetch", f"Read(/{agent.folder}/**)", f"Read(/{LAB_DIR}/**)"],
        "deny": ["Bash", *(f"Read(/{home / name}/**)" for name in SECRET_FOLDERS),
                 f"Edit(/{agent.workspace}/.claude/**)", f"Edit(/{LAB_DIR}/**)"]}}
    # Solo le regole qui sotto e quelle del suo spazio: le impostazioni dell'utente danno tutto.
    return ["--permission-mode", "acceptEdits", "--setting-sources", "project",
            "--settings", json.dumps(rules)]


class AgentJobs:
    def __init__(self, storage, claude_path: str, notify_carlo: Callable[[str], None],
                 agents: dict[str, Agent] | None = None, turn_factory=ClaudeTurn):
        self.storage, self.claude_path, self.notify_carlo = storage, claude_path, notify_carlo
        self.agents = discover_agents() if agents is None else agents
        self.turn_factory = turn_factory
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    def assign(self, key: str, task: str) -> tuple[bool, str]:
        agent = self.agents.get(key.lower())
        if agent is None:
            known = ", ".join(self.agents) or "nessuno"
            return False, f"agente sconosciuto: {key} (agenti disponibili: {known})"
        busy = self._lock(agent).locked()
        self._start(agent, task)
        log.info("Incarico a %s (%d caratteri)%s", agent.name, len(task), " in coda" if busy else "")
        return True, chat_texts.agent_assigned(agent.name, busy)

    def resume_interrupted(self) -> None:
        for agent in self.agents.values():
            task = self.storage.get_pending(self._storage_key(agent), STORAGE_CHAT)
            if task:
                log.info("Riprendo l'incarico di %s interrotto dal riavvio", agent.name)
                self._start(agent, chat_texts.AGENT_RESTART_NOTE + task, original=task)

    def _start(self, agent: Agent, task: str, original: str | None = None) -> None:
        job = asyncio.create_task(self._run(agent, task, original or task))
        self._tasks.add(job)
        job.add_done_callback(self._tasks.discard)

    async def _run(self, agent: Agent, prompt: str, task: str) -> None:
        key = self._storage_key(agent)
        async with self._lock(agent):
            self.storage.set_pending(key, STORAGE_CHAT, task)
            try:
                result = await self._turn(agent, prompt)
                if result.kind == "session_missing":
                    self.storage.clear_session(key, STORAGE_CHAT)
                    result = await self._turn(agent, prompt)
            except asyncio.TimeoutError:
                text = chat_texts.agent_timeout(agent.name, agent.max_minutes, task)
            except asyncio.CancelledError:
                raise  # spegnimento: l'incarico resta in sospeso e riparte all'accensione
            except Exception as error:
                log.exception("Incarico di %s fallito", agent.name)
                text = chat_texts.agent_failed(agent.name, task, f"errore interno: {error}")
            else:
                text = (chat_texts.agent_reply(agent.name, task, result.text) if result.kind == "ok"
                        else chat_texts.agent_failed(agent.name, task, result.text))
            self.storage.clear_pending(key, STORAGE_CHAT)
        self.notify_carlo(text)

    async def _turn(self, agent: Agent, prompt: str):
        key = self._storage_key(agent)
        turn = self.turn_factory(
            claude_path=self.claude_path, cwd=agent.workspace, prompt=prompt, model=agent.model,
            session_id=self.storage.get_session(key, STORAGE_CHAT),
            system_prompt=agent.instructions, log_name=f"{agent.key}/incarico",
            on_session=lambda session: self.storage.set_session(key, STORAGE_CHAT, session),
            permission_args=permission_args(agent))
        try:
            return await asyncio.wait_for(turn.run(), agent.max_minutes * 60)
        finally:
            turn.kill()

    def _lock(self, agent: Agent) -> asyncio.Lock:
        return self._locks.setdefault(agent.key, asyncio.Lock())

    @staticmethod
    def _storage_key(agent: Agent) -> str:
        return f"agente:{agent.key}"
