"""Curses interface for creating and running ASPIS campaigns.

Start it with ``python -m src.front_end --aspis-path /path/to/aspis``.
The configuration editor deliberately exposes the same concepts and choices as
``grammar.lark``; it does not maintain a second, incompatible configuration
format.
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import re
import shlex
import shutil
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from src.campaign_runner import CampaignRun
from src.config_parser import parse_runner_config
from src.env import Environment


DATA_MODES = ("eddi", "seddi", "fdsc", "none")
CONTROL_MODES = ("cfcss", "rasm", "inter-rasm", "none")
INJECTION_MODES = ("register", "memory", "global")
SOURCE_SUFFIXES = {".c", ".cpp", ".rs"}
WORD = re.compile(r"^[A-Za-z0-9_-]+$")
TOOLCHAIN_CONFIG = ".aspis-tui.json"
DEFAULT_ASPIS_PATH = "/mnt/misto/projects/ASPIS/aspis.sh"
DEFAULT_LLVM_PATH = "/home/cnx/llvm-21.1.8/build/bin"


class AspisTui:
    """A small keyboard-first interface, kept independent from terminal setup."""

    def __init__(self, environment: Environment, start_dir: Path | None = None):
        self.environment = environment
        self.start_dir = (start_dir or Path.cwd()).resolve()
        self.screen: curses.window | None = None

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, screen: curses.window) -> None:
        self.screen = screen
        curses.curs_set(0)
        screen.keypad(True)
        self._init_colors()
        self._home()

    def _init_colors(self) -> None:
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(2, curses.COLOR_CYAN, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)

    def _home(self) -> None:
        choice = 0
        choices = [
            "Esegui test",
            "Crea configurazione",
            "Configura toolchain",
            "Visualizza risultati campagna",
            "Esci",
        ]
        while True:
            self._frame("ASPIS · Campaign console", "↑↓ seleziona · Invio conferma · q esce")
            self._write(3, 3, "Toolchain attiva", curses.A_BOLD)
            self._write(4, 3, _aspis_command_preview(self.environment), curses.color_pair(2))
            self._write(6, 3, "Scegli un'operazione", curses.A_BOLD)
            for index, label in enumerate(choices):
                marker = "›" if index == choice else " "
                attr = self._selected_attr() if index == choice else 0
                self._write(8 + index * 2, 5, f"{marker} {label}", attr)
            key = self._key()
            if key in (ord("q"), 27) or (
                key in (curses.KEY_ENTER, 10, 13) and choice == 4
            ):
                return
            if key == curses.KEY_UP:
                choice = (choice - 1) % len(choices)
            elif key == curses.KEY_DOWN:
                choice = (choice + 1) % len(choices)
            elif key in (curses.KEY_ENTER, 10, 13):
                if choice == 0:
                    self._run_test()
                elif choice == 1:
                    self._create_configuration()
                elif choice == 2:
                    self._configure_toolchain()
                elif choice == 3:
                    self._view_campaign_results()

    def _configure_toolchain(self) -> None:
        values = {
            "aspis_path": str(self.environment.aspis_path),
            "llvm_path": str(self.environment.llvm_path),
            "vanilla_compiler": self.environment.vanilla_compiler,
            "debugger": self.environment.gdb_executable,
            "aspis_flags": shlex.join(self.environment.aspis_flags),
        }
        fields = (
            ("aspis_path", "Script ASPIS"),
            ("llvm_path", "Directory bin LLVM"),
            ("vanilla_compiler", "Compiler Oracle"),
            ("debugger", "Debugger"),
            ("aspis_flags", "Flag ASPIS aggiuntive"),
        )
        selected = 0
        while True:
            candidate = _environment_from_values(values)
            self._frame(
                "ASPIS · Toolchain",
                "↑↓ seleziona · Invio modifica/salva · Esc annulla",
            )
            self._write(2, 3, "Configurazione del progetto", curses.A_BOLD)
            for index, (key, label) in enumerate(fields):
                row = 4 + index * 2
                attr = self._selected_attr() if selected == index else 0
                marker = "›" if selected == index else " "
                value = values[key] or "(non configurato)"
                self._write(row, 3, f"{marker} {label}", attr)
                self._write(row + 1, 6, value, curses.color_pair(2))
            save_row = 4 + len(fields) * 2
            save_attr = self._selected_attr() if selected == len(fields) else curses.A_BOLD
            self._write(
                save_row,
                3,
                f"{'›' if selected == len(fields) else ' '} Salva configurazione",
                save_attr,
            )
            self._write(save_row + 2, 3, "Comando risultante", curses.A_BOLD)
            self._write(
                save_row + 3,
                3,
                _aspis_command_preview(candidate),
                curses.color_pair(2),
            )
            key_code = self._key()
            if key_code == 27:
                return
            if key_code == curses.KEY_UP:
                selected = (selected - 1) % (len(fields) + 1)
            elif key_code == curses.KEY_DOWN:
                selected = (selected + 1) % (len(fields) + 1)
            elif key_code in (curses.KEY_ENTER, 10, 13):
                if selected < len(fields):
                    key, label = fields[selected]
                    value = self._ask_text(
                        label,
                        values[key],
                        allow_empty=key == "aspis_flags",
                    )
                    if value is not None:
                        if key == "aspis_flags":
                            try:
                                shlex.split(value)
                            except ValueError as error:
                                self._message("Flag ASPIS non valide", str(error), error=True)
                                continue
                        values[key] = value
                    continue

                errors = _toolchain_errors(candidate)
                if errors:
                    self._message(
                        "Toolchain non valida", "\n".join(errors), error=True
                    )
                    continue
                settings_path = self.start_dir / TOOLCHAIN_CONFIG
                _save_toolchain(settings_path, candidate)
                self.environment = candidate
                self._message(
                    "Toolchain salvata",
                    f"{settings_path}\n\n{_aspis_command_preview(candidate)}",
                )
                return

    def _run_test(self) -> None:
        config = self._file_picker("Scegli il file di configurazione")
        if config is None:
            return
        try:
            campaign = parse_runner_config(config)
        except Exception as error:
            self._message("Configurazione non valida", str(error), error=True)
            return

        self._frame("ASPIS · Esecuzione", "Attendi: compilazione, analisi e injection possono richiedere tempo")
        self._write(3, 3, f"Esecuzione di {config.name}…", curses.A_BOLD)
        self.screen.refresh()
        try:
            with _working_directory(config.parent):
                runner = CampaignRun(campaign, self.environment, config.with_suffix(".log"))
                runner.run_campaign()
        except Exception as error:
            self._message("Esecuzione interrotta", str(error), error=True)
            return
        self._campaign_summary(config.name, runner.results)

    def _results(self, config: Path, results: list[dict], log: list[str]) -> None:
        if not results:
            self._paged_text("ASPIS · Risultati", log or ["Nessun risultato prodotto."], "Esc torna al menu")
            return
        selected = 0
        while True:
            result = results[selected]
            lines = _result_lines(result)
            footer = "←→ test · l log completo · Esc menu"
            self._frame(f"ASPIS · Risultati · {config.name} ({selected + 1}/{len(results)})", footer)
            for row, line in enumerate(_fit_lines(lines, self._width() - 6, self._height() - 5), start=3):
                self._write(row, 3, line)
            key = self._key()
            if key in (27, ord("q")):
                return
            if key == curses.KEY_LEFT:
                selected = (selected - 1) % len(results)
            elif key == curses.KEY_RIGHT:
                selected = (selected + 1) % len(results)
            elif key in (ord("l"), ord("L")):
                self._paged_text("ASPIS · Log", log, "↑↓ scorri · Esc torna ai risultati")

    def _view_campaign_results(self) -> None:
        result_file = self._file_picker("Seleziona i risultati della campagna (.json)", suffixes={".json"})
        if result_file is None:
            return
        try:
            results = json.loads(result_file.read_text(encoding="utf-8"))
        except Exception as error:
            self._message("Errore", f"Impossibile leggere il file: {error}", error=True)
            return
        self._campaign_summary(result_file.name, results)

    def _campaign_summary(self, name: str, results: list[dict]) -> None:
        if not results:
            self._message("Nessun risultato", "La campagna non contiene risultati.")
            return
        selected = 0
        while True:
            result = results[selected]
            injections = result.get("injections", {})
            total = injections.get("total", 0)
            
            def pct(count):
                if total == 0: return "0%"
                return f"{(count / total) * 100:.1f}%"

            lines = [
                f"CAMPAGNA: {name} - TEST: {result['name']} ({selected + 1}/{len(results)})",
                "",
                "CONFIGURAZIONE & OVERHEADS",
            ]
            
            diff = result.get("difference", {})
            for field, label in [("binary_size_bytes", "Dimensione binario"), ("execution_time_seconds", "Tempo esecuzione")]:
                delta = diff.get(field, {})
                percent = delta.get("percent")
                percent_str = "n/d" if percent is None else f"{percent:+.2f}%"
                lines.append(f"  {label:25}: {percent_str}")
            
            lines.extend([
                "",
                "RISULTATI INJECTION",
                f"  Injections Totali         : {total}",
                f"  Masked                    : {injections.get('masked', 0):<5} ({pct(injections.get('masked', 0))})",
                f"  Latent (Stato discorde)   : {injections.get('latent', 0):<5} ({pct(injections.get('latent', 0))})",
                f"  SDC (Silent Data Corrupt) : {injections.get('sdc', 0):<5} ({pct(injections.get('sdc', 0))})",
                f"  Timeouts (Loop infinito)  : {injections.get('timeout', 0):<5} ({pct(injections.get('timeout', 0))})",
                f"  Crashes (HW, Segfault)    : {injections.get('crash', 0):<5} ({pct(injections.get('crash', 0))})",
                f"  ASPIS Data (Rilevati)     : {injections.get('aspis_data', 0):<5} ({pct(injections.get('aspis_data', 0))})",
                f"  ASPIS CF (Rilevati)       : {injections.get('aspis_cf', 0):<5} ({pct(injections.get('aspis_cf', 0))})",
            ])

            footer = "←→ cambia test · s ispeziona SDC · l ispeziona Latent · Esc torna indietro"
            self._frame("ASPIS · Sommario Campagna", footer)
            for row, line in enumerate(_fit_lines(lines, self._width() - 6, self._height() - 5), start=3):
                self._write(row, 3, line)
            
            key = self._key()
            if key in (27, ord("q")):
                return
            if key == curses.KEY_LEFT:
                selected = (selected - 1) % len(results)
            elif key == curses.KEY_RIGHT:
                selected = (selected + 1) % len(results)
            elif key in (ord("s"), ord("S")):
                self._sdc_inspector(name, result, state_key="sdc_states", title_prefix="SDC")
            elif key in (ord("l"), ord("L")):
                self._sdc_inspector(name, result, state_key="latent_states", title_prefix="LATENT")

    def _sdc_inspector(self, name: str, result: dict, state_key: str = "sdc_states", title_prefix: str = "SDC") -> None:
        sdc_states = result.get("injections", {}).get(state_key, [])
        if not sdc_states:
            self._message(f"Nessun {title_prefix}", f"Non ci sono risultati da ispezionare per questo test.")
            return

        oracle = result.get("oracle", {})
        selected = 0
        while True:
            state = sdc_states[selected]
            lines = [
                f"ISPEZIONE {title_prefix} ({selected + 1}/{len(sdc_states)}) - Test: {result['name']}",
                "",
                "DIFFERENZE RISPETTO ALL'ORACLE:",
            ]
            
            # STDOUT
            if state.get("stdout") != oracle.get("stdout"):
                lines.extend([
                    "--- STDOUT ---",
                    f"- ORACLE: {oracle.get('stdout', '')!r}",
                    f"+ ASPIS:  {state.get('stdout', '')!r}",
                    ""
                ])
                
            # STDERR
            if state.get("stderr") != oracle.get("stderr"):
                lines.extend([
                    "--- STDERR ---",
                    f"- ORACLE: {oracle.get('stderr', '')!r}",
                    f"+ ASPIS:  {state.get('stderr', '')!r}",
                    ""
                ])
                
            # VARIABLES
            oracle_vars = oracle.get("variables", {})
            aspis_vars = state.get("variables", {})
            all_vars = sorted(set(oracle_vars) | set(aspis_vars))
            var_lines = []
            for var in all_vars:
                if oracle_vars.get(var) != aspis_vars.get(var):
                    var_lines.append(f"  {var}: ORACLE={oracle_vars.get(var, '<missing>')} ASPIS={aspis_vars.get(var, '<missing>')}")
            if var_lines:
                lines.append("--- VARIABILI ---")
                lines.extend(var_lines)
                lines.append("")

            footer = "←→ scorri SDC · Esc torna al sommario"
            self._frame("ASPIS · SDC Inspector", footer)
            for row, line in enumerate(_fit_lines(lines, self._width() - 6, self._height() - 5), start=3):
                if line.startswith("- "):
                    self._write(row, 3, line, curses.color_pair(3))
                elif line.startswith("+ "):
                    self._write(row, 3, line, curses.color_pair(2))
                else:
                    self._write(row, 3, line)

            key = self._key()
            if key in (27, ord("q")):
                return
            if key == curses.KEY_LEFT:
                selected = (selected - 1) % len(sdc_states)
            elif key == curses.KEY_RIGHT:
                selected = (selected + 1) % len(sdc_states)

    def _create_configuration(self) -> None:
        name = self._ask_text("Nome del test", "test")
        if name is None:
            return
        if not WORD.fullmatch(name):
            self._message("Nome non valido", "Usa solo lettere, numeri, _ o -.", error=True)
            return
        programs = self._file_picker("Seleziona i programmi (.c, .cpp, .rs)", multi=True, suffixes=SOURCE_SUFFIXES)
        if not programs:
            return
        parents = {path.parent for path in programs}
        if len(parents) != 1:
            self._message("Programmi in cartelle diverse", "La grammatica accetta solo nomi file: seleziona file nella stessa cartella.", error=True)
            return
        source_dir = parents.pop()
        data_mode = self._choose("Protezione dati", DATA_MODES)
        if data_mode is None:
            return
        control_mode = self._choose("Protezione control flow", CONTROL_MODES)
        if control_mode is None:
            return
        timeout = self._ask_positive("Timeout in millisecondi", "5000")
        repetitions = self._ask_positive("Ripetizioni dell'esecuzione", "100")
        if timeout is None or repetitions is None:
            return
        compiler_flags = self._ask_text(
            "Compiler/linker flags (separate da virgola; es. -O2, -lm)", ""
        )
        if compiler_flags is None:
            return
        injection = self._choose("Modalità injection", INJECTION_MODES)
        if injection is None:
            return
        injection_line = injection
        if injection == "register":
            data_only = self._choose("Inietta solo dati?", ("false", "true"))
            if data_only is None:
                return
            injection_line += f" data_only: {data_only}"
        elif injection == "global":
            target = self._ask_text("Variabile globale target (vuoto = nessuna)", "")
            if target is None or (target and not WORD.fullmatch(target)):
                self._message("Variabile non valida", "Usa solo lettere, numeri, _ o -.", error=True)
                return
            if target:
                injection_line += f" target_global: {target}"
        functions = self._ask_text("Funzioni/variabili, es. main(x:4), helper (vuoto = nessuna)", "")
        if functions is None:
            return
        filename = self._ask_text("Nome file configurazione", "campaign.cfg")
        if not filename:
            return
        target = source_dir / filename
        text = _configuration_text(name, [path.name for path in programs], data_mode, control_mode,
                                   timeout, repetitions, compiler_flags, injection_line, functions)
        try:
            _validate_config_text(text)
        except Exception as error:
            self._message("Configurazione non valida", str(error), error=True)
            return
        target.write_text(text, encoding="utf-8")
        self._message("Configurazione salvata", f"{target}\n\nOra puoi selezionarla da “Esegui test”.")

    def _file_picker(self, title: str, multi: bool = False, suffixes: set[str] | None = None):
        current = self.start_dir
        selected: set[Path] = set()
        index = 0
        offset = 0
        while True:
            try:
                entries = [current.parent] + sorted(current.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower()))
            except OSError as error:
                self._message("Cartella non accessibile", str(error), error=True)
                return None
            entries = [path for path in entries if path == current.parent or path.is_dir() or suffixes is None or path.suffix.lower() in suffixes]
            index = min(index, max(0, len(entries) - 1))
            view_height = self._height() - 7
            if index < offset:
                offset = index
            if index >= offset + view_height:
                offset = index - view_height + 1
            self._frame(title, "↑↓ muovi · Invio apri/conferma · Spazio seleziona · Esc annulla")
            self._write(2, 3, str(current), curses.color_pair(2))
            for row, path in enumerate(entries[offset:offset + view_height], start=4):
                actual = offset + row - 4
                label = "[..]" if path == current.parent else (f"[{path.name}]" if path.is_dir() else path.name)
                check = "✓ " if path in selected else "  "
                self._write(row, 3, check + label, self._selected_attr() if actual == index else 0)
            key = self._key()
            if key in (27, ord("q")):
                return None
            if key == curses.KEY_UP:
                index = max(0, index - 1)
            elif key == curses.KEY_DOWN:
                index = min(len(entries) - 1, index + 1)
            elif key == ord(" ") and multi and entries[index].is_file():
                path = entries[index]
                selected.symmetric_difference_update({path})
            elif key in (curses.KEY_ENTER, 10, 13):
                path = entries[index]
                if path.is_dir():
                    current, index, offset = path.resolve(), 0, 0
                elif multi:
                    selected.add(path)
                    return sorted(selected)
                else:
                    return path

    def _choose(self, title: str, options: Iterable[str]) -> str | None:
        options = tuple(options)
        selected = 0
        while True:
            self._frame("ASPIS · Configurazione", "↑↓ seleziona · Invio conferma · Esc annulla")
            self._write(3, 3, title, curses.A_BOLD)
            for index, option in enumerate(options):
                self._write(5 + index * 2, 5, f"{'›' if selected == index else ' '} {option}", self._selected_attr() if selected == index else 0)
            key = self._key()
            if key == 27:
                return None
            if key == curses.KEY_UP:
                selected = (selected - 1) % len(options)
            elif key == curses.KEY_DOWN:
                selected = (selected + 1) % len(options)
            elif key in (curses.KEY_ENTER, 10, 13):
                return options[selected]

    def _ask_positive(self, title: str, default: str) -> str | None:
        value = self._ask_text(title, default)
        if value is None:
            return None
        if not value.isdigit() or int(value) < 1:
            self._message("Valore non valido", "Inserisci un intero positivo.", error=True)
            return None
        return value

    def _ask_text(
        self, title: str, default: str, allow_empty: bool = False
    ) -> str | None:
        self._frame("ASPIS · Configurazione", "Invio conferma · Esc annulla")
        self._write(3, 3, title, curses.A_BOLD)
        if default:
            self._write(4, 3, f"Predefinito: {default}", curses.color_pair(2))
        self._write(5, 3, "> ")
        self.screen.move(5, 5)
        curses.curs_set(1)
        curses.echo()
        try:
            raw = self.screen.getstr(5, 5, max(1, self._width() - 7))
            if raw == b"\x1b":
                return None
            value = raw.decode("utf-8").strip()
        except curses.error:
            value = ""
        finally:
            curses.noecho()
            curses.curs_set(0)
        if value or allow_empty:
            return value
        return default

    def _paged_text(self, title: str, lines: list[str], footer: str) -> None:
        offset = 0
        while True:
            self._frame(title, footer)
            height = self._height() - 5
            visible = _fit_lines(lines[offset:], self._width() - 6, height)
            for row, line in enumerate(visible, start=3):
                self._write(row, 3, line)
            key = self._key()
            if key in (27, ord("q")):
                return
            if key == curses.KEY_UP:
                offset = max(0, offset - 1)
            elif key == curses.KEY_DOWN:
                offset = min(max(0, len(lines) - 1), offset + 1)

    def _message(self, title: str, text: str, error: bool = False) -> None:
        self._frame(f"ASPIS · {title}", "Invio o Esc torna indietro")
        attr = curses.color_pair(3) if error else 0
        for row, line in enumerate(_fit_lines(text.splitlines(), self._width() - 6, self._height() - 5), start=3):
            self._write(row, 3, line, attr)
        while self._key() not in (27, 10, 13, curses.KEY_ENTER):
            pass

    def _frame(self, title: str, footer: str) -> None:
        self.screen.erase()
        self._write(0, 2, title, curses.A_BOLD)
        self._write(self._height() - 2, 2, footer, curses.color_pair(2))
        self.screen.hline(1, 0, curses.ACS_HLINE, self._width() - 1)
        self.screen.hline(self._height() - 3, 0, curses.ACS_HLINE, self._width() - 1)
        self.screen.refresh()

    def _write(self, row: int, col: int, text: str, attr: int = 0) -> None:
        try:
            self.screen.addnstr(row, col, text, max(0, self._width() - col - 1), attr)
        except curses.error:
            pass

    def _key(self) -> int:
        return self.screen.getch()

    def _width(self) -> int:
        return self.screen.getmaxyx()[1]

    def _height(self) -> int:
        return self.screen.getmaxyx()[0]

    def _selected_attr(self) -> int:
        return curses.color_pair(1) | curses.A_BOLD


def _expanded_path(value: str) -> str:
    return str(Path(value).expanduser().resolve()) if value else ""


def _environment_from_values(values: dict[str, str]) -> Environment:
    debugger = values.get("debugger", "") or "gdb"
    return Environment(
        aspis_path=_expanded_path(values.get("aspis_path", "")),
        vanilla_compiler=values.get("vanilla_compiler", "") or "clang",
        llvm_path=_expanded_path(values.get("llvm_path", "")),
        gdb_executable=debugger,
        lldb_executable=debugger,
        aspis_flags=shlex.split(values.get("aspis_flags", "")),
    )


def _toolchain_payload(environment: Environment) -> dict:
    return {
        "aspis_path": str(environment.aspis_path),
        "llvm_path": str(environment.llvm_path),
        "vanilla_compiler": environment.vanilla_compiler,
        "debugger": environment.gdb_executable,
        "aspis_flags": list(environment.aspis_flags),
    }


def _save_toolchain(path: Path, environment: Environment) -> None:
    path.write_text(
        json.dumps(_toolchain_payload(environment), indent=2) + "\n",
        encoding="utf-8",
    )


def _load_toolchain(path: Path, fallback: Environment) -> Environment:
    if not path.is_file():
        return fallback
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Impossibile leggere {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{path} deve contenere un oggetto JSON.")
    flags = payload.get("aspis_flags", [])
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
        raise ValueError("aspis_flags deve essere una lista di stringhe.")
    values = {
        "aspis_path": str(payload.get("aspis_path", fallback.aspis_path)),
        "llvm_path": str(payload.get("llvm_path", fallback.llvm_path)),
        "vanilla_compiler": str(
            payload.get("vanilla_compiler", fallback.vanilla_compiler)
        ),
        "debugger": str(payload.get("debugger", fallback.gdb_executable)),
        "aspis_flags": shlex.join(flags),
    }
    return _environment_from_values(values)


def _toolchain_errors(environment: Environment) -> list[str]:
    errors = []
    aspis_path = Path(environment.aspis_path)
    if not aspis_path.is_file():
        errors.append(f"Script ASPIS non trovato: {aspis_path}")
    elif not os.access(aspis_path, os.X_OK):
        errors.append(f"Script ASPIS non eseguibile: {aspis_path}")

    llvm_path = Path(environment.llvm_path)
    if not llvm_path.is_dir():
        errors.append(f"Directory LLVM non trovata: {llvm_path}")
    else:
        missing = [
            executable
            for executable in ("clang", "opt", "llvm-link")
            if not (llvm_path / executable).is_file()
        ]
        if missing:
            errors.append(f"Binari LLVM mancanti: {', '.join(missing)}")

    if shutil.which(environment.vanilla_compiler) is None:
        errors.append(f"Compiler Oracle non trovato: {environment.vanilla_compiler}")
    if shutil.which(environment.gdb_executable) is None:
        errors.append(f"Debugger non trovato: {environment.gdb_executable}")
    return errors


def _aspis_command_preview(environment: Environment) -> str:
    command = [str(environment.aspis_path)]
    if environment.llvm_path:
        command.extend(["--llvm-bin", str(environment.llvm_path)])
    command.extend(environment.aspis_flags)
    command.extend(["<modalità>", "<sorgente.c>"])
    return shlex.join(command)


def _configuration_text(name, programs, data_mode, control_mode, timeout, repetitions, flags, injection, functions) -> str:
    lines = [
        f"name: {name}", f"programs: [{', '.join(programs)}]",
        f"data_protection: {data_mode}", f"control_flow_protection: {control_mode}",
        f"timeout_ms: {timeout}", f"execution_repetitions: {repetitions}",
    ]
    if flags:
        lines.append(f"compiler_flags: [{flags}]")
    lines.append(f"injection: {injection}")
    if functions:
        lines.append(f"functions: [{functions}]")
    return "\n".join(lines) + "\n"


def _validate_config_text(text: str) -> None:
    with NamedTemporaryFile("w", suffix=".cfg", encoding="utf-8", delete=False) as handle:
        handle.write(text)
        path = Path(handle.name)
    try:
        parse_runner_config(path)
    finally:
        path.unlink(missing_ok=True)


def _result_lines(result: dict) -> list[str]:
    oracle, aspis, difference = result["oracle"], result["aspis"], result["difference"]
    lines = [f"TEST  {result['name']}", "", "ESITO", f"  return code     oracle {oracle['returncode']}   ASPIS {aspis['returncode']}",
             f"  stdout          {'uguale' if oracle['stdout'] == aspis['stdout'] else 'DIFFERENTE'}",
             f"  stderr          {'uguale' if oracle['stderr'] == aspis['stderr'] else 'DIFFERENTE'}", "", "MISURE"]
    for field, unit in (("execution_time_seconds", "s"), ("binary_size_bytes", "B")):
        delta = difference[field]
        percentage = "n/d" if delta["percent"] is None else f"{delta['percent']:+.2f}%"
        lines.append(f"  {field}: oracle {oracle[field]:.6g} {unit} · ASPIS {aspis[field]:.6g} {unit} · Δ {delta['absolute']:+.6g} ({percentage})")
    lines.extend(["", "VARIABILI"])
    names = sorted(set(oracle.get("variables", {})) | set(aspis.get("variables", {})))
    lines.extend(f"  {'OK' if oracle.get('variables', {}).get(name) == aspis.get('variables', {}).get(name) else 'DIFF':4} {name}: {oracle.get('variables', {}).get(name, '<missing>')} → {aspis.get('variables', {}).get(name, '<missing>')}" for name in names)
    return lines


def _fit_lines(lines: Iterable[str], width: int, height: int) -> list[str]:
    output = []
    for line in lines:
        chunks = [line[index:index + max(1, width)] for index in range(0, max(1, len(line)), max(1, width))] or [""]
        output.extend(chunks)
        if len(output) >= height:
            return output[:height]
    return output


@contextmanager
def _working_directory(directory: Path):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


def main() -> None:
    parser = argparse.ArgumentParser(description="ASPIS curses interface")
    parser.add_argument("--aspis-path", default=os.environ.get("ASPIS_PATH"))
    parser.add_argument("--compiler", default=os.environ.get("ASPIS_COMPILER"))
    parser.add_argument("--llvm-path", default=os.environ.get("ASPIS_LLVM_PATH"))
    parser.add_argument("--debugger", default=os.environ.get("ASPIS_DEBUGGER"))
    parser.add_argument("--aspis-flags", default=os.environ.get("ASPIS_FLAGS"))
    args = parser.parse_args()
    start_dir = Path.cwd().resolve()
    defaults = Environment(
        aspis_path=DEFAULT_ASPIS_PATH,
        vanilla_compiler="clang",
        llvm_path=DEFAULT_LLVM_PATH,
    )
    try:
        saved = _load_toolchain(start_dir / TOOLCHAIN_CONFIG, defaults)
        environment = _environment_from_values(
            {
                "aspis_path": args.aspis_path or str(saved.aspis_path),
                "llvm_path": args.llvm_path or str(saved.llvm_path),
                "vanilla_compiler": args.compiler or saved.vanilla_compiler,
                "debugger": args.debugger or saved.gdb_executable,
                "aspis_flags": (
                    args.aspis_flags
                    if args.aspis_flags is not None
                    else shlex.join(saved.aspis_flags)
                ),
            }
        )
    except ValueError as error:
        parser.error(str(error))
    AspisTui(environment, start_dir=start_dir).run()


if __name__ == "__main__":
    main()
