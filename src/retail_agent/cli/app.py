"""Interactive CLI (contracts §6.4, DD §6).

Slash commands control the session only; everything else goes to the agent as natural language.
Without a TTY the CLI prints plain text (no spinner) so sessions can be scripted through stdin.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal, Sequence, TextIO

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from retail_agent.agent.graph import AgentApp, Deps, error_for, make_checkpointer
from retail_agent.agent.state import (
    AssistantResponse, ConfirmRequest, ErrorEvent, FinalResponse, ProgressEvent, RateLimitWait, ReportSummary,
)
from retail_agent.agent.threads import ThreadRegistry
from retail_agent.analysis.output import AnalysisOutput, format_cell
from retail_agent.config import ConfigError, Settings, load_catalog, load_settings, load_text
from retail_agent.llm.wrapper import LLMClient
from retail_agent.reports.store import ReportStore
from retail_agent.security.identity import IdentityStore, UnknownExecutiveError
from retail_agent.tracing import Tracer

log = logging.getLogger(__name__)

CommandName = Literal["login", "new", "resume", "trace", "feedback", "quit", "help"]
_COMMANDS: frozenset[str] = frozenset({"login", "new", "resume", "trace", "feedback", "quit", "help"})

HELP = ("Commands: /login <exec_id> · /new · /resume <thread_id> · /trace · "
        "/feedback good|bad [comment] · /quit · /help\n"
        "Everything else is a question; listing or deleting saved reports is also asked in plain words.")
THREAD_NOT_FOUND = "Thread not found."
SECRET_ENV_VARS = ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY")
ERROR_LOG_NAME = "agent.log"   # written next to the trace file (logs/, git-ignored)


@dataclass(frozen=True)
class Command:
    name: CommandName
    args: tuple[str, ...]


def parse_command(line: str) -> Command | None:
    """None = not a slash command. An unknown command becomes `help`."""
    text = line.strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    name = parts[0].lower() if parts else ""
    if name not in _COMMANDS:
        return Command("help", ())
    return Command(name, tuple(parts[1:]))  # type: ignore[arg-type]


# -- rendering --------------------------------------------------------------------------------

def _report_table(reports: Sequence[ReportSummary]) -> Table:
    table = Table(show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Title")
    table.add_column("Saved")
    table.add_column("Id", style="dim")
    for i, r in enumerate(reports, 1):
        table.add_row(str(i), r.title, r.created_at[:10], r.id)
    return table


def _hidden_rows(result) -> str | None:
    hidden = (result.total_rows or len(result.rows)) - len(result.rows)
    return f"{hidden} more row{'s' if hidden != 1 else ''} not shown" if hidden > 0 else None


def render_analysis(console: Console, out: AnalysisOutput) -> None:
    """Summary first, then findings and actions as tables, then definitions and limitations."""
    if out.outcome == "clarification" and out.clarification_question:
        console.print(Markdown(out.clarification_question))
        return
    console.print(Markdown(out.summary.strip()))
    if out.findings:
        table = Table(title="Findings", title_justify="left")
        table.add_column("Id")
        table.add_column("Finding")
        table.add_column("Queries", style="dim")
        for f in out.findings:
            table.add_row(f.id, f.statement, ", ".join(f.query_ids))
        console.print(table)
    for result in out.tables:
        if not result.columns:
            continue
        table = Table(title=result.title, title_justify="left")
        for column in result.columns:
            table.add_column(column)
        for row in result.rows:
            table.add_row(*(format_cell(v) for v in row))
        console.print(table)
        hidden = _hidden_rows(result)
        if hidden:
            console.print(hidden, style="dim", markup=False)
    if out.action_items:
        table = Table(title="Recommended actions", title_justify="left")
        table.add_column("Action")
        table.add_column("Based on", style="dim")
        for a in out.action_items:
            table.add_row(a.action, a.finding_id)
        console.print(table)
    for title, items in (("Definitions used", out.definitions_used), ("Limitations", out.limitations)):
        if items:
            console.print(Markdown(f"**{title}**\n" + "\n".join(f"- {x}" for x in items)))


def render_response(console: Console, response: AssistantResponse) -> None:
    for note in response.notes:
        console.print(note, style="yellow", markup=False)
    if response.analysis is not None:
        render_analysis(console, response.analysis)
    elif response.text:
        console.print(Markdown(response.text))
    if response.reports:
        console.print(_report_table(response.reports))


def render_confirm(console: Console, request: ConfirmRequest) -> None:
    console.print(_report_table(request.reports))
    console.print(request.prompt, style="bold yellow", markup=False)


# -- session ----------------------------------------------------------------------------------

class Session:
    """The read-eval-print loop. `read` returns the next input line, or None at end of input."""

    def __init__(self, app: AgentApp, console: Console, read: Callable[[str], str | None], *,
                 interactive: bool) -> None:
        self.app = app
        self.console = console
        self.read = read
        self.interactive = interactive

    def prompt(self) -> str:
        who = self.app.current_exec.exec_id if self.app.current_exec else "not logged in"
        return f"[{who}] > "

    def run(self) -> int:
        self.console.print("Retail analytics agent. Type /help for commands.", markup=False)
        while True:
            try:
                line = self.read(self.prompt())
            except (EOFError, KeyboardInterrupt):
                line = None
            if line is None:
                return 0
            line = line.strip()
            if not line:
                continue
            if not self.interactive:
                self.console.print(f"{self.prompt()}{line}", markup=False, highlight=False)
            command = parse_command(line)
            if command is None:
                self.turn(line)
            elif command.name == "quit":
                self.record("quit")
                return 0
            else:
                self.command(command)

    # -- commands

    def record(self, action: str) -> None:
        """`session` trace record (contracts §1.3)."""
        app = self.app
        app.deps.tracer.bind(thread_id=app.current_thread_id,
                             exec_id=app.current_exec.exec_id if app.current_exec else None
                             ).write("session", detail={"action": action})

    def say(self, text: str, style: str | None = None) -> None:
        self.console.print(text, style=style, markup=False, highlight=False)

    def command(self, cmd: Command) -> None:
        app = self.app
        if cmd.name == "help":
            self.say(HELP)
        elif cmd.name == "login":
            if len(cmd.args) != 1:
                self.say("Usage: /login <exec_id>")
                return
            try:
                executive = app.login(cmd.args[0])
            except UnknownExecutiveError:
                self.say(f"Unknown executive '{cmd.args[0]}'.", "red")
                return
            self.say(f"Logged in as {executive.display_name} ({executive.exec_id}). New conversation started.")
            self.record("login")
        elif cmd.name == "new":
            try:
                thread_id = app.new_thread()
            except RuntimeError as exc:
                self.say(str(exc))
                return
            self.say(f"New conversation started ({thread_id}).")
            self.record("new")
        elif cmd.name == "resume":
            if len(cmd.args) != 1:
                self.say("Usage: /resume <thread_id>")
            elif app.resume(cmd.args[0]):
                self.say(f"Resumed conversation {cmd.args[0]}.")
                self.record("resume")
            else:
                self.say(THREAD_NOT_FOUND)
        elif cmd.name == "trace":
            thread_id, path = app.trace_info()
            self.say(f"thread_id: {thread_id or '(none yet)'}\ntrace log: {path}\n"
                     f"error log: {path.parent / ERROR_LOG_NAME}")
        elif cmd.name == "feedback":
            rating = cmd.args[0].lower() if cmd.args else ""
            if rating not in ("good", "bad"):
                self.say("Usage: /feedback good|bad [comment]")
                return
            if app.current_exec is None:
                self.say("Please log in first.")
                return
            app.feedback(rating, " ".join(cmd.args[1:]) or None)  # type: ignore[arg-type]
            self.say("Thanks, feedback recorded.")

    # -- one turn

    def turn(self, text: str) -> None:
        if self.app.current_exec is None:   # no executive, no scope: nothing reaches the agent
            self.say("Please log in first: /login <exec_id>", "yellow")
            return
        final: FinalResponse | ConfirmRequest | ErrorEvent | None = None
        try:
            for event in self._events(text):
                if isinstance(event, (FinalResponse, ConfirmRequest, ErrorEvent)):
                    final = event
        except KeyboardInterrupt:
            self.say("Turn interrupted.", "yellow")
            return
        except Exception as exc:  # defence in depth: AgentApp.send already maps its own failures
            log.exception("Turn failed")
            message, _ = error_for(exc, self.app.current_thread_id or "unknown")
            final = ErrorEvent(message, "internal")
        if isinstance(final, FinalResponse):
            render_response(self.console, final.response)
        elif isinstance(final, ConfirmRequest):
            render_confirm(self.console, final)
        elif isinstance(final, ErrorEvent):
            self.say(final.message, "red")

    def _events(self, text: str) -> Iterator[object]:
        """Yield every event of the turn, showing progress (spinner on a TTY, plain lines otherwise)."""
        stream = self.app.send(text)
        if not self.interactive:
            for event in stream:
                note = _progress_text(event)
                if note:
                    self.say(f"... {note}")
                yield event
            return
        with self.console.status("Working…") as status:
            for event in stream:
                note = _progress_text(event)
                if note:
                    status.update(f"{note}…")
                yield event


def _progress_text(event: object) -> str | None:
    if isinstance(event, ProgressEvent):
        return event.step
    if isinstance(event, RateLimitWait):
        return f"Waiting for rate limit ({event.seconds:.0f} s)"
    return None


# -- wiring -----------------------------------------------------------------------------------

def build_app(settings: Settings) -> AgentApp:
    from retail_agent.bq_client import create_runner

    content = settings.paths.content_dir
    secrets = [v for v in (os.environ.get(n) for n in SECRET_ENV_VARS) if v]
    tracer = Tracer(settings.paths.traces, max_field_chars=settings.tracing_max_field_chars,
                    secret_values=secrets)
    app_db = settings.paths.app_db
    deps = Deps(
        settings=settings, catalog=load_catalog(content / "catalog.yaml"),
        glossary=load_text(content / "glossary.md"), tracer=tracer,
        identity=IdentityStore(app_db), threads=ThreadRegistry(app_db), reports=ReportStore(app_db),
        bq=create_runner(settings), llm=LLMClient(settings))
    return AgentApp(deps, make_checkpointer(settings))


def configure_logging(settings: Settings) -> Path | None:
    """Diagnostics go to logs/agent.log, never into the chat.

    A failed turn shows the user one short message with its thread ID; the traceback is written
    to this file, next to the trace log, where the thread ID finds it. Returns the file, or None
    if it cannot be opened (logging is then dropped rather than printed into the chat)."""
    path = settings.paths.traces.parent / ERROR_LOG_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        logging.basicConfig(level=logging.WARNING, handlers=[logging.NullHandler()])
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    return path


def _make_reader(stdin: TextIO, console: Console, interactive: bool) -> Callable[[str], str | None]:
    if interactive:
        return lambda prompt: console.input(prompt)

    def read(prompt: str) -> str | None:
        line = stdin.readline()
        return line if line else None
    return read


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m retail_agent", description="Retail analytics agent")
    parser.add_argument("--exec", dest="exec_id", help="log in as this executive at start")
    parser.add_argument("--profile", help="LLM profile (overrides LLM_PROFILE), e.g. free")
    args = parser.parse_args(argv)
    if args.profile:
        os.environ["LLM_PROFILE"] = args.profile

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    console = Console(force_terminal=None if interactive else False)
    try:
        settings = load_settings()
        configure_logging(settings)
        app = build_app(settings)
    except ConfigError as exc:
        console.print(f"Configuration problem: {exc}", markup=False)
        return 2
    session = Session(app, console, _make_reader(sys.stdin, console, interactive), interactive=interactive)
    if args.exec_id:
        session.command(Command("login", (args.exec_id,)))
        if app.current_exec is None:
            return 2
    elif not app.deps.identity.list_executives():
        console.print("No executives found. Run `python scripts/seed_executives.py` first.", markup=False)
    return session.run()
