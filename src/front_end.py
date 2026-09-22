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
import math
import os
import re
import shlex
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from itertools import product
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from src.campaign_runner import CampaignRun
from src.config_builder import campaign_to_text
from src.constants import DEFAULT_TIMEOUT_MS
from src.config_parser import (
    Campaign,
    GlobalInjection,
    MemoryInjection,
    RegisterInjection,
    RunnerTest,
    parse_runner_config,
)
from src.env import Environment
from src.file_io import atomic_write_text
from src.source_symbols import analyze_source, SourceAnalysisError


DATA_MODES = ("eddi", "seddi", "fdsc", "none")
CONTROL_MODES = ("cfcss", "rasm", "inter-rasm", "none")
INJECTION_MODES = ("register", "memory", "global")
SOURCE_SUFFIXES = {".c", ".cpp", ".rs"}
WORD = re.compile(r"^[A-Za-z0-9_-]+$")
TOOLCHAIN_CONFIG = ".aspis-tui.json"
DEFAULT_ASPIS_PATH = "/mnt/misto/projects/ASPIS/aspis.sh"
DEFAULT_LLVM_PATH = "/home/cnx/llvm-21.1.8/build/bin"


class _QuitTui(Exception):
    """Signal a ``:q`` command from any nested TUI screen."""


class FileSelection(list[Path]):
    """Paths selected by the picker, including an optional bulk directory."""

    def __init__(self, paths: Iterable[Path], batch_directory: Path | None = None):
        super().__init__(paths)
        self.batch_directory = batch_directory


@dataclass
class ConfigurationDraft:
    name: str = "test"
    programs: list[str] = field(default_factory=list)
    batch_directory: str = ""
    data_mode: str | list[str] = field(default_factory=lambda: ["eddi"])
    control_mode: str | list[str] = field(default_factory=lambda: ["cfcss"])
    timeout: str = str(DEFAULT_TIMEOUT_MS)
    repetitions: str = "100"
    measure_overhead: bool = True
    measurement_repetitions: str = ""
    compiler_flags: str = "-lm"
    injection: str | list[str] = field(default_factory=lambda: ["register data_only: false"])
    functions: str | dict[str, str] = "main"


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
        try:
            self._home()
        except _QuitTui:
            return

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
            "Run campaign",
            "Create configuration",
            "Edit configuration",
            "Configure toolchain",
            "View campaign results",
            "Exit",
        ]
        while True:
            self._frame("ASPIS · Campaign console", "↑↓/jk select · Enter confirm")
            roomy = self._height() >= 24
            if roomy:
                self._write(3, 3, "Active toolchain", curses.A_BOLD)
                self._write(4, 3, _aspis_command_preview(self.environment), curses.color_pair(2))
            self._write(6 if roomy else 2, 3, "Choose an action", curses.A_BOLD)
            first_row, step = (8, 2) if roomy else (3, 1)
            visible = max(1, (self._height() - 4 - first_row) // step + 1)
            offset = max(0, choice - visible + 1)
            for index in range(offset, min(len(choices), offset + visible)):
                label = choices[index]
                marker = "›" if index == choice else " "
                attr = self._selected_attr() if index == choice else 0
                self._write(first_row + (index - offset) * step, 5, f"{marker} {label}", attr)
            key = self._key()
            if key in (curses.KEY_ENTER, 10, 13) and choice == 5:
                return
            if key in (curses.KEY_UP, ord("k")):
                choice = (choice - 1) % len(choices)
            elif key in (curses.KEY_DOWN, ord("j")):
                choice = (choice + 1) % len(choices)
            elif key in (curses.KEY_ENTER, 10, 13):
                if choice == 0:
                    self._run_test()
                elif choice == 1:
                    self._create_configuration()
                elif choice == 2:
                    self._edit_configuration()
                elif choice == 3:
                    self._configure_toolchain()
                elif choice == 4:
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
            ("llvm_path", "LLVM bin directory"),
            ("vanilla_compiler", "Oracle compiler"),
            ("debugger", "Debugger"),
            ("aspis_flags", "Additional ASPIS flags"),
        )
        selected = 0
        while True:
            candidate = _environment_from_values(values)
            self._frame(
                "ASPIS · Toolchain",
                "↑↓/jk select · Enter edit/save · Esc cancel",
            )
            self._write(2, 3, "Project settings", curses.A_BOLD)
            visible = max(1, (self._height() - 7) // 2)
            offset = max(0, selected - visible + 1)
            for index in range(offset, min(len(fields) + 1, offset + visible)):
                row = 4 + (index - offset) * 2
                attr = self._selected_attr() if selected == index else 0
                marker = "›" if selected == index else " "
                if index == len(fields):
                    self._write(row, 3, f"{marker} Save settings", attr or curses.A_BOLD)
                else:
                    key, label = fields[index]
                    self._write(row, 3, f"{marker} {label}", attr)
                    self._write(row + 1, 6, values[key] or "(not configured)", curses.color_pair(2))
            if self._height() >= 23:
                self._write(16, 3, "Effective command", curses.A_BOLD)
                self._write(17, 3, _aspis_command_preview(candidate), curses.color_pair(2))
            key_code = self._key()
            if key_code == 27:
                return
            if key_code in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % (len(fields) + 1)
            elif key_code in (curses.KEY_DOWN, ord("j")):
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
                                self._message("Invalid ASPIS flags", str(error), error=True)
                                continue
                        values[key] = value
                    continue

                errors = _toolchain_errors(candidate)
                if errors:
                    self._message(
                        "Invalid toolchain", "\n".join(errors), error=True
                    )
                    continue
                settings_path = self.start_dir / TOOLCHAIN_CONFIG
                try:
                    _save_toolchain(settings_path, candidate)
                except OSError as error:
                    self._message("Unable to save settings", str(error), error=True)
                    continue
                self.environment = candidate
                self._message(
                    "Toolchain settings saved",
                    f"{settings_path}\n\n{_aspis_command_preview(candidate)}",
                )
                return

    def _run_test(self) -> None:
        configs = self._file_picker(
            "Select one or more configuration files", multi=True, suffixes={".cfg"}
        )
        if not configs:
            return
        if isinstance(configs, Path):
            configs = [configs]
        for config in configs:
            self._run_configuration(config)

    def _run_configuration(self, config: Path) -> None:
        try:
            campaign = parse_runner_config(config)
        except Exception as error:
            try:
                with config.with_suffix(".log").open("a", encoding="utf-8") as log:
                    log.write(f"\nERROR: CONFIGURATION [{config}]: {error}\n")
            except OSError as log_error:
                self._message("Unable to write configuration log", str(log_error), error=True)
            self._message("Invalid configuration", str(error), error=True)
            return

        self._frame(
            "ASPIS · Running campaign",
            "Please wait: compilation, analysis, and fault injection may take time",
            exit_hint=False,
        )
        self._write(3, 3, f"Running {config.name}…", curses.A_BOLD)
        self.screen.refresh()
        try:
            with _working_directory(config.parent):
                runner = CampaignRun(campaign, self.environment, config.with_suffix(".log"))
                self._run_with_progress(runner, config)
        except Exception as error:
            self._message("Campaign interrupted", str(error), error=True)
            return
        if not runner.results:
            self._paged_text(
                "ASPIS · Campaign failed",
                runner.log or ["The campaign produced no results."],
                "↑↓/jk scroll · Esc return to file selection",
            )
            return
        self._campaign_summary(config.name, runner.results, runner.log)

    def _run_with_progress(self, runner: CampaignRun, config: Path) -> None:
        """Keep curses on the main thread while the campaign reports events."""
        events = Queue()
        started = time.monotonic()
        updated = started
        state = {"index": 0, "total": len(runner.campaign.runs),
                 "test": "Starting…", "phase": "Preparing campaign", "sources": ""}
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(runner.run_campaign, progress=events.put)
            while True:
                try:
                    state = events.get(timeout=0.2)
                    updated = time.monotonic()
                    while True:
                        state = events.get_nowait()
                except Empty:
                    pass
                now = time.monotonic()
                elapsed = int(now - started)
                spinner = "|/-\\"[int((now - started) * 4) % 4]
                self._frame("ASPIS · Running campaign", "Campaign running · results appear when finished", exit_hint=False)
                lines = [
                    f"{spinner} {config.name}",
                    f"Test {state['index']}/{state['total']}: {state['test']}",
                    f"Phase: {state['phase']}",
                    f"Elapsed: {elapsed // 60:02d}:{elapsed % 60:02d} · Last progress: {int(now - updated)} s ago",
                    f"Sources: {state['sources']}",
                ]
                for row, line in enumerate(_fit_lines(lines, self._width() - 6, self._height() - 5), start=3):
                    self._write(row, 3, line)
                self.screen.refresh()
                if future.done():
                    future.result()
                    return

    def _results(self, config: Path, results: list[dict], log: list[str]) -> None:
        if not results:
            self._paged_text(
                "ASPIS · Results",
                log or ["The campaign produced no results."],
                "Esc return to menu",
            )
            return
        selected = 0
        while True:
            result = results[selected]
            lines = _result_lines(result)
            footer = "←→/hl test · g full log · Esc return to menu"
            self._frame(f"ASPIS · Results · {config.name} ({selected + 1}/{len(results)})", footer)
            for row, line in enumerate(_fit_lines(lines, self._width() - 6, self._height() - 5), start=3):
                self._write(row, 3, line)
            key = self._key()
            if key in (27, ord("q")):
                return
            if key in (curses.KEY_LEFT, ord("h")):
                selected = (selected - 1) % len(results)
            elif key in (curses.KEY_RIGHT, ord("l")):
                selected = (selected + 1) % len(results)
            elif key in (ord("g"), ord("G")):
                self._paged_text("ASPIS · Log", log, "↑↓/jk scroll · Esc return to results")

    def _view_campaign_results(self) -> None:
        result_file = self._file_picker(
            "Select campaign results (.json)", suffixes={".json"}
        )
        if result_file is None:
            return
        try:
            results = json.loads(result_file.read_text(encoding="utf-8"))
            _validate_results(results)
        except Exception as error:
            self._message("Error", f"Unable to read the file: {error}", error=True)
            return
        self._campaign_summary(result_file.name, results)

    def _campaign_summary(
        self, name: str, results: list[dict], log: list[str] | None = None
    ) -> None:
        if not results:
            self._message("No results", "This campaign contains no results.")
            return
        selected = 0
        offset = 0
        while True:
            result = results[selected]
            injections = result.get("injections", {})
            total = injections.get("total", 0)
            
            def pct(count):
                if total == 0: return "0%"
                return f"{(count / total) * 100:.1f}%"

            lines = [
                f"CAMPAIGN: {name} - TEST: {result['name']} ({selected + 1}/{len(results)})",
                "",
                "CONFIGURATION & OVERHEAD",
                f"  Per-process timeout       : {result.get('timeout_ms', 'n/a')} ms",
            ]
            
            diff = result.get("difference", {})
            for field, label in [
                ("binary_size_bytes", "Binary size"),
                ("execution_time_seconds", "Execution time"),
            ]:
                delta = diff.get(field, {})
                percent = delta.get("percent")
                percent_str = "n/a" if percent is None else f"{percent:+.2f}%"
                lines.append(f"  {label:25}: {percent_str}")
            
            lines.extend([
                "",
                "FAULT INJECTION RESULTS",
                f"  Total injections          : {total}",
                f"  Masked                    : {injections.get('masked', 0):<5} ({pct(injections.get('masked', 0))})",
                f"  Latent (State mismatch)   : {injections.get('latent', 0):<5} ({pct(injections.get('latent', 0))})",
                f"  SDC (Silent Data Corrupt) : {injections.get('sdc', 0):<5} ({pct(injections.get('sdc', 0))})",
                f"  Timeouts (Infinite loop)  : {injections.get('timeout', 0):<5} ({pct(injections.get('timeout', 0))})",
                f"  Crashes (HW, Segfault)    : {injections.get('crash', 0):<5} ({pct(injections.get('crash', 0))})",
                f"  ASPIS Data (Detected)     : {injections.get('aspis_data', 0):<5} ({pct(injections.get('aspis_data', 0))})",
                f"  ASPIS CF (Detected)       : {injections.get('aspis_cf', 0):<5} ({pct(injections.get('aspis_cf', 0))})",
            ])

            error_count = sum(line.startswith("ERROR:") for line in (log or []))
            if error_count:
                lines.insert(
                    1,
                    f"WARNING: {error_count} test(s) failed · press g to view the log",
                )
            footer = "↑↓/jk scroll · ←→/hl test · s SDC · t Latent"
            if log is not None:
                footer += " · g log"
            footer += " · Esc go back"
            self._frame("ASPIS · Campaign summary", footer)
            wrapped = _wrapped_lines(lines, self._width() - 6)
            height = max(1, self._height() - 6)
            offset = min(offset, max(0, len(wrapped) - height))
            for row, line in enumerate(wrapped[offset:offset + height], start=3):
                self._write(row, 3, line)
            
            key = self._key()
            if key in (27, ord("q")):
                return
            if key in (curses.KEY_LEFT, ord("h")):
                selected = (selected - 1) % len(results)
                offset = 0
            elif key in (curses.KEY_RIGHT, ord("l")):
                selected = (selected + 1) % len(results)
                offset = 0
            elif key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                offset += 1
            elif key in (ord("s"), ord("S")):
                self._sdc_inspector(name, result, state_key="sdc_states", title_prefix="SDC")
            elif key in (ord("t"), ord("T")):
                self._sdc_inspector(name, result, state_key="latent_states", title_prefix="LATENT")
            elif key in (ord("g"), ord("G")) and log is not None:
                self._paged_text(
                    "ASPIS · Campaign log",
                    log,
                    "↑↓/jk scroll · Esc return to summary",
                )

    def _sdc_inspector(self, name: str, result: dict, state_key: str = "sdc_states", title_prefix: str = "SDC") -> None:
        sdc_states = result.get("injections", {}).get(state_key, [])
        if not sdc_states:
            self._message(
                f"No {title_prefix} results",
                "There are no results to inspect for this test.",
            )
            return

        oracle = result.get("oracle", {})
        selected = 0
        offset = 0
        while True:
            state = sdc_states[selected]
            lines = [
                f"{title_prefix} INSPECTION ({selected + 1}/{len(sdc_states)}) - Test: {result['name']}",
                "",
                "DIFFERENCES FROM ORACLE:",
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
            oracle_vars = oracle.get("snapshots", {}).get(
                state.get("function"), oracle
            ).get("variables", {})
            aspis_vars = state.get("variables", {})
            all_vars = sorted(set(oracle_vars) | set(aspis_vars))
            var_lines = []
            for var in all_vars:
                if oracle_vars.get(var) != aspis_vars.get(var):
                    var_lines.append(f"  {var}: ORACLE={oracle_vars.get(var, '<missing>')} ASPIS={aspis_vars.get(var, '<missing>')}")
            if var_lines:
                lines.append("--- VARIABLES ---")
                lines.extend(var_lines)
                lines.append("")

            footer = f"↑↓/jk scroll · ←→/hl browse {title_prefix} · Esc return"
            self._frame(f"ASPIS · {title_prefix} Inspector", footer)
            wrapped = _wrapped_lines(lines, self._width() - 6)
            height = max(1, self._height() - 6)
            offset = min(offset, max(0, len(wrapped) - height))
            for row, line in enumerate(wrapped[offset:offset + height], start=3):
                if line.startswith("- "):
                    self._write(row, 3, line, curses.color_pair(3))
                elif line.startswith("+ "):
                    self._write(row, 3, line, curses.color_pair(2))
                else:
                    self._write(row, 3, line)

            key = self._key()
            if key in (27, ord("q")):
                return
            if key in (curses.KEY_LEFT, ord("h")):
                selected = (selected - 1) % len(sdc_states)
                offset = 0
            elif key in (curses.KEY_RIGHT, ord("l")):
                selected = (selected + 1) % len(sdc_states)
                offset = 0
            elif key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                offset += 1

    def _create_configuration(self) -> None:
        draft = ConfigurationDraft()
        self._edit_draft_field(draft, "programs")
        if not draft.programs:
            return

        draft = self._configuration_form(
            draft,
            "ASPIS · New configuration",
        )
        if draft is None:
            return

        destination = self._file_picker(
            "Choose where to save the configuration",
            select_directory=True,
        )
        if destination is None:
            return
        filename = self._ask_text("Configuration file name", "campaign.cfg")
        if not filename:
            return
        if not filename.endswith(".cfg"):
            filename += ".cfg"
        target = destination / filename
        try:
            text = _draft_text(draft)
            _validate_config_text(text)
            try:
                with target.open("x", encoding="utf-8") as handle:
                    handle.write(text)
            except FileExistsError:
                if self._choose(f"Replace existing configuration {target.name}?",
                                ("Cancel", "Replace")) != "Replace":
                    return
                atomic_write_text(target, text)
        except OSError as error:
            self._message("Unable to save", str(error), error=True)
            return
        self._message(
            "Configuration saved",
            f"{target}\n\n",
        )

    def _edit_configuration(self) -> None:
        config = self._file_picker(
            "Select a configuration to edit", suffixes={".cfg"}
        )
        if config is None:
            return
        try:
            campaign = parse_runner_config(config)
        except Exception as error:
            self._message("Invalid configuration", str(error), error=True)
            return

        index = 0
        if len(campaign.runs) > 1:
            labels = [
                f"{position + 1}. {run.name}"
                for position, run in enumerate(campaign.runs)
            ]
            selected = self._choose("Select a test to edit", labels)
            if selected is None:
                return
            index = labels.index(selected)

        draft = self._configuration_form(
            _draft_from_run(campaign.runs[index]),
            f"ASPIS · Edit · {config.name}",
            allow_delete=True
        )
        if draft is None:
            return
        try:
            if draft == "DELETE":
                if len(campaign.runs) == 1:
                    self._message("Cannot delete last test",
                                  "A configuration must contain at least one test.")
                    return
                if self._choose(f"Delete test {campaign.runs[index].name}?",
                                ("Cancel", "Delete")) != "Delete":
                    return
                runs = [
                    *campaign.runs[:index],
                    *campaign.runs[index + 1 :],
                ]
                edited_runs = []
            else:
                edited_runs = _campaign_from_draft(draft).runs
                runs = [
                    *campaign.runs[:index],
                    *edited_runs,
                    *campaign.runs[index + 1 :],
                ]
            text = campaign_to_text(Campaign(runs=runs))
            _validate_config_text(text)
            atomic_write_text(config, text)
        except _QuitTui:
            raise
        except Exception as error:
            self._message("Unable to save", str(error), error=True)
            return
        
        if draft == "DELETE":
            self._message("Configuration updated", "Test deleted successfully.")
        else:
            self._message(
                "Configuration updated",
                f"{config}\n\n{len(edited_runs)} test entries saved.",
            )

    def _configuration_form(
        self, draft: ConfigurationDraft, title: str, allow_delete: bool = False
    ) -> ConfigurationDraft | str | None:
        fields = (
            ("name", "Name"),
            ("programs", "Source files"),
            ("data_mode", "Data protection"),
            ("control_mode", "Control-flow protection"),
            ("timeout", "Per-process timeout (ms)"),
            ("repetitions", "Execution repetitions"),
            ("measurement_repetitions", "Measurement repetitions"),
            ("measure_overhead", "Measure overhead"),
            ("compiler_flags", "Compiler/linker flags"),
            ("injection", "Fault injection"),
            ("functions", "Functions/variables"),
        )
        selected = 0
        while True:
            self._frame(
                title,
                "↑↓/jk field · Enter edit/save · Esc go back",
            )
            save_index = len(fields)
            delete_index = save_index + 1 if allow_delete else -1
            max_index = save_index + (1 if allow_delete else 0)

            visible = max(1, (self._height() - 5) // 2)
            offset = max(0, selected - visible + 1)
            for index in range(offset, min(max_index + 1, offset + visible)):
                row = 2 + (index - offset) * 2
                marker = "›" if selected == index else " "
                attr = self._selected_attr() if selected == index else 0
                if index < len(fields):
                    key, label = fields[index]
                    self._write(row, 3, f"{marker} {label}", attr)
                    self._write(row + 1, 6, _draft_field_value(draft, key) or "(empty)", curses.color_pair(2))
                else:
                    label = "Validate and save" if index == save_index else "Delete test"
                    self._write(row, 3, f"{marker} {label}", attr or curses.A_BOLD)

            key_code = self._key()
            if key_code == 27:
                return None
            if key_code in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % (max_index + 1)
            elif key_code in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % (max_index + 1)
            elif key_code in (curses.KEY_ENTER, 10, 13):
                if selected == delete_index:
                    return "DELETE"
                if selected == save_index:
                    errors = _draft_errors(draft)
                    if not errors:
                        try:
                            _validate_config_text(_draft_text(draft))
                        except Exception as error:
                            errors.append(str(error))
                    if errors:
                        self._message(
                            "Invalid configuration",
                            "\n".join(errors),
                            error=True,
                        )
                        continue
                    return draft
                self._edit_draft_field(draft, fields[selected][0])

    def _edit_draft_field(self, draft: ConfigurationDraft, key: str) -> None:
        if key == "programs":
            mode = self._choose(
                "Source selection",
                ("Select files", "Link files together", "Batch directory"),
            )
            if mode == "Batch directory":
                self._select_batch_directory(draft)
            elif mode in {"Select files", "Link files together"}:
                programs = self._file_picker(
                    "Select source files",
                    multi=True,
                    suffixes=SOURCE_SUFFIXES,
                )
                if programs:
                    draft.programs = [str(path.resolve()) for path in programs]
                    bulk_directory = getattr(programs, "batch_directory", None)
                    if mode == "Select files" and len(programs) > 1:
                        parents = {path.resolve().parent for path in programs}
                        independent_root = (
                            bulk_directory.resolve()
                            if bulk_directory
                            else next(iter(parents)) if len(parents) == 1 else self.start_dir
                        )
                        draft.batch_directory = str(independent_root)
                    else:
                        draft.batch_directory = ""
            return
        if key == "data_mode":
            value = self._choose_many(
                "Data protection (one or more)", DATA_MODES, _mode_values(draft.data_mode)
            )
        elif key == "control_mode":
            value = self._choose_many(
                "Control-flow protection (one or more)",
                CONTROL_MODES,
                _mode_values(draft.control_mode),
            )
        elif key == "injection":
            value = self._edit_injection(_mode_values(draft.injection))
        elif key == "functions":
            self._scope_menu(draft)
            return
        elif key == "measure_overhead":
            choice = self._choose("Measure overhead", ["Yes", "No"])
            if choice:
                value = choice == "Yes"
            else:
                value = None
        else:
            labels = {
                "name": "Test name",
                "timeout": "Per-process timeout in milliseconds",
                "repetitions": "Execution repetitions",
                "measurement_repetitions": "Measurement reps (empty = same as execution reps)",
                "compiler_flags": "Comma-separated compiler/linker flags",
                "functions": "Functions/variables, e.g. main(x:4), helper",
            }
            value = self._ask_text(
                labels[key],
                str(getattr(draft, key)),
                allow_empty=key in {"compiler_flags", "functions", "measurement_repetitions"},
            )
        if value is not None:
            setattr(draft, key, value)

    def _scope_menu(self, draft: ConfigurationDraft) -> None:
        scopes = {p: draft.functions.get(p, "main")
                  if isinstance(draft.functions, dict) else draft.functions
                  for p in draft.programs}
        while True:
            labels = {f"{i + 1}. {p} · {scopes[p]}": p
                      for i, p in enumerate(draft.programs)}
            choice = self._choose("Scopes per file (default: main)",
                                  [*labels, "Use main for all files", "Done"])
            if choice is None or choice == "Done":
                draft.functions = scopes
                return
            if choice == "Use main for all files":
                scopes = dict.fromkeys(draft.programs, "main")
                continue
            program = labels[choice]
            mode = self._choose(Path(program).name,
                                ("Browse source symbols", "Enter manually", "Reset to main"))
            if mode == "Browse source symbols":
                value = self._source_scope(Path(program), scopes[program])
            elif mode == "Enter manually":
                value = self._ask_text("Scope: main(x, @G)", scopes[program])
            elif mode == "Reset to main":
                value = "main"
            else:
                continue
            if value is not None:
                scopes[program] = value

    def _source_scope(self, path: Path, current: str) -> str | None:
        try:
            symbols = analyze_source(path)
            if symbols.warnings:
                self._message("Source analysis limitations", "\n".join(symbols.warnings))
            if not symbols.functions:
                raise SourceAnalysisError("No unambiguous functions available.")
        except SourceAnalysisError as error:
            self._message("Source analysis unavailable", str(error), error=True)
            return self._ask_text(f"Manual scope for {path.name}: main(x, @G)", current)
        labels = {f"{f.name} · line {f.line}": f for f in symbols.functions}
        chosen = self._choose_many(f"{path.name}: functions", labels)
        if chosen is None:
            return None
        scopes = []
        for label in chosen:
            function = labels[label]
            options = [*function.variables, *("@" + name for name in symbols.globals)]
            variables = self._choose_many(
                f"{function.name}: targets (locals / @globals); Enter allows none",
                options, allow_empty=True,
            ) if options else []
            if variables is None:
                return None
            scopes.append(function.name + ("(" + ", ".join(variables) + ")" if variables else ""))
        return self._ask_text(f"Confirm scope for {path.name} (optional array length: x:4)", ", ".join(scopes))

    def _edit_injection(self, current: list[str]) -> list[str] | None:
        return self._choose_many(
            "Fault injection",
            ("register data_only: false", "register data_only: true", "memory", "global"),
            current, toggle_enter=True,
            labels={
                "register data_only: false": "Register · all registers",
                "register data_only: true": "Register · data registers only",
                "memory": "Memory · local variables",
                "global": "Global · global variables",
            },
        )

    def _select_batch_directory(self, draft: ConfigurationDraft) -> bool:
        directory = self._file_picker(
            "Select a batch directory", select_directory=True
        )
        if directory is None:
            return False
        programs = sorted(
            path.resolve()
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES
        )
        if not programs:
            self._message(
                "No source files found",
                "The selected directory contains no .c, .cpp, or .rs files.",
                error=True,
            )
            return False
        draft.batch_directory = str(directory.resolve())
        draft.programs = [str(path) for path in programs]
        return True

    def _file_picker(
        self,
        title: str,
        multi: bool = False,
        suffixes: set[str] | None = None,
        select_directory: bool = False,
    ):
        current = self.start_dir
        selected: set[Path] = set()
        bulk_directory: Path | None = None
        index = 0
        offset = 0
        while True:
            try:
                entries = [current.parent] + sorted(current.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower()))
            except OSError as error:
                self._message("Unable to open directory", str(error), error=True)
                return None
            entries = [
                path
                for path in entries
                if path == current.parent
                or path.is_dir()
                or (
                    not select_directory
                    and (suffixes is None or path.suffix.lower() in suffixes)
                )
            ]
            index = min(index, max(0, len(entries) - 1))
            view_height = self._height() - 7
            if index < offset:
                offset = index
            if index >= offset + view_height:
                offset = index - view_height + 1
            footer = (
                "↑↓/jk move · h parent · Enter/l open · s select directory · Esc cancel"
                if select_directory
                else "↑↓/jk move · h parent · Enter/l open · Space select · s all · Esc cancel"
            )
            self._frame(title, footer)
            self._write(2, 3, str(current), curses.color_pair(2))
            for row, path in enumerate(entries[offset:offset + view_height], start=4):
                actual = offset + row - 4
                label = "[..]" if path == current.parent else (f"[{path.name}]" if path.is_dir() else path.name)
                check = f"[{'✓' if path in selected else ' '}] " if multi and path.is_file() else "    "
                self._write(row, 3, check + label, self._selected_attr() if actual == index else 0)
            key = self._key()
            if key in (27, ord("q")):
                return None
            if select_directory and key in (ord("s"), ord("S")):
                return current
            if multi and not select_directory and key in (ord("s"), ord("S")):
                files = {path for path in entries if path.is_file()}
                if files:
                    return FileSelection(sorted(files), current)
                continue
            if key == ord("h"):
                current, index, offset = current.parent.resolve(), 0, 0
            elif key in (curses.KEY_UP, ord("k")):
                index = max(0, index - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                index = min(len(entries) - 1, index + 1)
            elif key == ord(" ") and multi and entries[index].is_file():
                path = entries[index]
                selected.symmetric_difference_update({path})
                bulk_directory = None
            elif key in (curses.KEY_ENTER, 10, 13, ord("l")):
                path = entries[index]
                if path.is_dir():
                    current, index, offset = path.resolve(), 0, 0
                elif select_directory:
                    continue
                elif multi:
                    was_selected = path in selected
                    selected.add(path)
                    if not was_selected:
                        bulk_directory = None
                    return FileSelection(sorted(selected), bulk_directory)
                else:
                    return path

    def _choose(self, title: str, options: Iterable[str]) -> str | None:
        options = tuple(options)
        selected = 0
        while True:
            self._frame(
                "ASPIS · Configuration",
                "↑↓/jk select · Enter confirm · Esc cancel",
            )
            self._write(3, 3, title, curses.A_BOLD)
            height = max(1, self._height() - 9)
            offset = max(0, selected - height + 1)
            for index in range(offset, min(len(options), offset + height)):
                option = options[index]
                self._write(5 + index - offset, 5, f"{'›' if selected == index else ' '} {option}", self._selected_attr() if selected == index else 0)
            key = self._key()
            if key == 27:
                return None
            if key in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(options)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(options)
            elif key in (curses.KEY_ENTER, 10, 13):
                return options[selected]

    def _choose_many(
        self, title: str, options: Iterable[str], current: Iterable[str] = (), allow_empty: bool = False,
        toggle_enter: bool = False, labels: dict[str, str] | None = None,
    ) -> list[str] | None:
        options = tuple(options)
        checked = set(current)
        row_count = len(options) + int(toggle_enter)
        selected = 0
        while True:
            self._frame(
                "ASPIS · Configuration",
                ("↑↓/jk move · Enter/Space toggle · Done confirm · Esc cancel"
                 if toggle_enter else "↑↓/jk move · Space toggle · Enter confirm · Esc cancel"),
            )
            self._write(3, 3, title, curses.A_BOLD)
            height = max(1, self._height() - 9)
            offset = max(0, selected - height + 1)
            for index in range(offset, min(row_count, offset + height)):
                option = options[index] if index < len(options) else "Done"
                marker = "✓" if option in checked else " "
                cursor = "›" if selected == index else " "
                self._write(
                    5 + index - offset,
                    5,
                    f"{cursor} [{marker}] {(labels or {}).get(option, option)}",
                    self._selected_attr() if selected == index else 0,
                )
            key = self._key()
            if key == 27:
                return None
            if key in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % max(1, row_count)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % max(1, row_count)
            elif selected < len(options) and (key == ord(" ") or
                    (toggle_enter and key in (curses.KEY_ENTER, 10, 13))):
                checked.symmetric_difference_update({options[selected]})
            elif key in (curses.KEY_ENTER, 10, 13) or (toggle_enter and key == ord(" ")):
                if checked or allow_empty:
                    return [option for option in options if option in checked]
                self._message("Selection required", "Select at least one option.", error=True)

    def _ask_positive(self, title: str, default: str) -> str | None:
        value = self._ask_text(title, default)
        if value is None:
            return None
        if not value.isdigit() or int(value) < 1:
            self._message(
                "Invalid value", "Enter a positive integer.", error=True
            )
            return None
        return value

    def _ask_text(
        self, title: str, default: str, allow_empty: bool = False
    ) -> str | None:
        value = list(default)
        cursor = len(value)
        curses.curs_set(1)
        curses.noecho()
        try:
            while True:
                self._frame("ASPIS · Configuration", "Enter save · Esc cancel · Ctrl-U clear")
                self._write(3, 3, title, curses.A_BOLD)
                self._write(4, 3, "Empty value allowed" if allow_empty else "Value required")
                width = max(1, self._width() - 7)
                offset = max(0, cursor - width + 1)
                self._write(5, 3, "> " + "".join(value[offset:offset + width]))
                self.screen.move(5, 5 + cursor - offset)
                key = self.screen.get_wch()
                if key == "\x1b":
                    return None
                if key in ("\n", "\r", curses.KEY_ENTER):
                    text = "".join(value).strip()
                    if text or allow_empty:
                        return text
                elif key == "q" and cursor and value[cursor - 1] == ":":
                    raise _QuitTui
                elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if cursor:
                        cursor -= 1
                        value.pop(cursor)
                elif key == curses.KEY_DC:
                    if cursor < len(value):
                        value.pop(cursor)
                elif key == "\x15":
                    value, cursor = [], 0
                elif key in (curses.KEY_HOME, "\x01"):
                    cursor = 0
                elif key in (curses.KEY_END, "\x05"):
                    cursor = len(value)
                elif key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                elif key == curses.KEY_RIGHT:
                    cursor = min(len(value), cursor + 1)
                elif isinstance(key, str) and key.isprintable():
                    value.insert(cursor, key)
                    cursor += 1
        finally:
            curses.curs_set(0)

    def _paged_text(self, title: str, lines: list[str], footer: str) -> None:
        offset = 0
        while True:
            self._frame(title, footer)
            height = max(1, self._height() - 6)
            wrapped = _wrapped_lines(lines, self._width() - 6)
            offset = min(offset, max(0, len(wrapped) - height))
            visible = wrapped[offset:offset + height]
            for row, line in enumerate(visible, start=3):
                self._write(row, 3, line)
            key = self._key()
            if key in (27, ord("q")):
                return
            if key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                offset = min(max(0, len(wrapped) - height), offset + 1)

    def _message(self, title: str, text: str, error: bool = False) -> None:
        if len(_wrapped_lines(text.splitlines(), self._width() - 6)) > max(1, self._height() - 6):
            self._paged_text(f"ASPIS · {title}", text.splitlines(), "↑↓/jk scroll · Esc go back")
            return
        self._frame(f"ASPIS · {title}", "Enter or Esc go back")
        attr = curses.color_pair(3) if error else 0
        for row, line in enumerate(_fit_lines(text.splitlines(), self._width() - 6, self._height() - 5), start=3):
            self._write(row, 3, line, attr)
        while self._key() not in (27, 10, 13, curses.KEY_ENTER):
            pass

    def _frame(self, title: str, footer: str, exit_hint: bool = True) -> None:
        if exit_hint:
            while self._height() < 9 or self._width() < 30:
                self.screen.erase()
                self._write(0, 0, "Resize to 30x9; :q exits")
                self.screen.refresh()
                self._key()
            footer = f"{footer} · :q exit app"
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
        key = self.screen.getch()
        if key == ord(":"):
            command = self.screen.getch()
            if command == ord("q"):
                raise _QuitTui
            return command
        return key

    def _width(self) -> int:
        return self.screen.getmaxyx()[1]

    def _height(self) -> int:
        return self.screen.getmaxyx()[0]

    def _selected_attr(self) -> int:
        return curses.color_pair(1) | curses.A_BOLD


def _validate_results(results) -> None:
    """Validate the fields consumed by summaries and state inspectors."""
    def mapping(value, label):
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object.")
        return value

    def number(value, label, count=False):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or (count and (type(value) is not int or value < 0))):
            raise ValueError(f"Invalid number for {label}.")

    def state(value, label):
        mapping(value, label)
        mapping(value.get("variables", {}), f"{label}.variables")
        if "function" in value and not isinstance(value["function"], str):
            raise ValueError(f"{label}.function must be a string.")

    if not isinstance(results, list):
        raise ValueError("Campaign results must be a list of tests.")
    for index, result in enumerate(results, 1):
        label = f"Test {index}"
        mapping(result, label)
        if not isinstance(result.get("name"), str) or not result["name"]:
            raise ValueError(f"{label} needs a name.")
        injections = mapping(result.get("injections", {}), f"{label}.injections")
        for key in ("total", "masked", "latent", "sdc", "timeout", "crash", "aspis_data", "aspis_cf"):
            number(injections.get(key, 0), f"{label}.{key}", count=True)
        for key in ("sdc_states", "latent_states"):
            states = injections.get(key, [])
            if not isinstance(states, list):
                raise ValueError(f"{label}.{key} must be a list.")
            for item in states:
                state(item, f"{label}.{key}")
        state(result.get("oracle", {}), f"{label}.oracle")
        snapshots = mapping(result.get("oracle", {}).get("snapshots", {}), f"{label}.snapshots")
        for snapshot in snapshots.values():
            state(snapshot, f"{label}.snapshot")
        difference = mapping(result.get("difference", {}), f"{label}.difference")
        for key in ("binary_size_bytes", "execution_time_seconds"):
            delta = mapping(difference.get(key, {}), f"{label}.{key}")
            if delta.get("percent") is not None:
                number(delta["percent"], f"{label}.{key}.percent")


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
    atomic_write_text(
        path,
        json.dumps(_toolchain_payload(environment), indent=2) + "\n",
    )


def _load_toolchain(path: Path, fallback: Environment) -> Environment:
    if not path.is_file():
        return fallback
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to read {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    flags = payload.get("aspis_flags", [])
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
        raise ValueError("aspis_flags must be a list of strings.")
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
        errors.append(f"ASPIS script not found: {aspis_path}")
    elif not os.access(aspis_path, os.X_OK):
        errors.append(f"ASPIS script is not executable: {aspis_path}")

    llvm_path = Path(environment.llvm_path)
    if not llvm_path.is_dir():
        errors.append(f"LLVM directory not found: {llvm_path}")
    else:
        missing = [
            executable
            for executable in ("clang", "opt", "llvm-link")
            if not (llvm_path / executable).is_file()
        ]
        if missing:
            errors.append(f"Missing LLVM binaries: {', '.join(missing)}")

    if shutil.which(environment.vanilla_compiler) is None:
        errors.append(f"Oracle compiler not found: {environment.vanilla_compiler}")
    if shutil.which(environment.gdb_executable) is None:
        errors.append(f"Debugger not found: {environment.gdb_executable}")
    return errors


def _aspis_command_preview(environment: Environment) -> str:
    command = [str(environment.aspis_path)]
    if environment.llvm_path:
        command.extend(["--llvm-bin", str(environment.llvm_path)])
    command.extend(environment.aspis_flags)
    command.extend(["<mode>", "<source.c>"])
    return shlex.join(command)


def _draft_field_value(draft: ConfigurationDraft, key: str) -> str:
    if key != "programs":
        value = getattr(draft, key)
        if key in {"data_mode", "control_mode", "injection"}:
            return ", ".join(_mode_values(value))
        if key == "functions" and isinstance(value, dict):
            return "(configured per-file)"
        return str(value)
    source_count = len(draft.programs)
    source_label = "file" if source_count == 1 else "files"
    if draft.batch_directory:
        return f"batch · {draft.batch_directory} · {source_count} {source_label}"
    if not draft.programs:
        return ""
    names = ", ".join(Path(program).name for program in draft.programs)
    return f"{source_count} {source_label} · {names}"


def _draft_errors(draft: ConfigurationDraft) -> list[str]:
    errors = []
    if not WORD.fullmatch(draft.name):
        errors.append("Name: use letters, numbers, underscores, or hyphens only.")
    if not draft.programs:
        errors.append("Select at least one source file.")
    if not _mode_values(draft.data_mode):
        errors.append("Select at least one data-protection mode.")
    if not _mode_values(draft.control_mode):
        errors.append("Select at least one control-flow-protection mode.")
    for program in draft.programs:
        path = Path(program)
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            errors.append(f"Unsupported source extension: {path}")
        elif not path.is_file():
            errors.append(f"Source file not found: {path}")
    for value, label in (
        (draft.timeout, "Timeout"),
        (draft.repetitions, "Execution repetitions"),
    ):
        if not value.isdigit() or int(value) < 1:
            errors.append(f"{label}: enter a positive integer.")
    if draft.measurement_repetitions and (not draft.measurement_repetitions.isdigit() or int(draft.measurement_repetitions) < 1):
        errors.append("Measurement repetitions: enter a positive integer or leave empty.")
    return errors


def _configuration_text(
    name,
    programs,
    data_mode,
    control_mode,
    timeout,
    repetitions,
    flags,
    injection,
    functions,
    measure_overhead=True,
    measurement_repetitions="",
) -> str:
    lines = [
        f"name: {name}",
        f"programs: [{', '.join(json.dumps(str(program)) for program in programs)}]",
        f"data_protection: {data_mode}",
        f"control_flow_protection: {control_mode}",
        f"timeout_ms: {timeout}",
        f"execution_repetitions: {repetitions}",
    ]
    if not measure_overhead:
        lines.append("measure_overhead: false")
    if measurement_repetitions:
        lines.append(f"measurement_repetitions: {measurement_repetitions}")
    if flags:
        lines.append(f"compiler_flags: [{flags}]")
    lines.append(f"injection: {injection}")
    if functions:
        lines.append(f"functions: [{functions}]")
    return "\n".join(lines) + "\n"


def _draft_text(draft: ConfigurationDraft) -> str:
    programs = draft.programs if draft.batch_directory else [draft.programs]
    modes = list(product(
        _mode_values(draft.data_mode), _mode_values(draft.control_mode), _mode_values(draft.injection)
    ))
    blocks = []
    used_names = set()
    multiple_modes = len(modes) > 1
    for index, item in enumerate(programs, start=1):
        run_programs = [item] if draft.batch_directory else item
        base_name = draft.name
        if draft.batch_directory:
            stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(item).stem).strip("-")
            base_name = f"{draft.name}-{stem or index}"
        for data_mode, control_mode, injection in modes:
            name = base_name
            if multiple_modes:
                name = f"{name}-{data_mode}-{control_mode}-{injection.split()[0]}"
            candidate = name
            duplicate = 2
            while candidate in used_names:
                candidate = f"{name}-{duplicate}"
                duplicate += 1
            name = candidate
            used_names.add(name)
            if isinstance(draft.functions, dict):
                file_functions = (draft.functions.get(item, "main") if draft.batch_directory
                                  else ", ".join(dict.fromkeys(
                                      draft.functions.get(p, "main") for p in run_programs)))
            else:
                file_functions = draft.functions
            blocks.append(
                _configuration_text(
                    name,
                    run_programs,
                    data_mode,
                    control_mode,
                    draft.timeout,
                    draft.repetitions,
                    draft.compiler_flags,
                    injection,
                    file_functions,
                    measure_overhead=draft.measure_overhead,
                    measurement_repetitions=draft.measurement_repetitions,
                ).rstrip()
            )
    return "\n".join(blocks) + "\n"


def _mode_values(value: str | Iterable[str]) -> list[str]:
    """Normalize old single-value drafts and new multi-value drafts."""
    return [value] if isinstance(value, str) else list(value)


def _campaign_from_draft(draft: ConfigurationDraft) -> Campaign:
    with NamedTemporaryFile("w", suffix=".cfg", encoding="utf-8", delete=False) as handle:
        handle.write(_draft_text(draft))
        path = Path(handle.name)
    try:
        return parse_runner_config(path)
    finally:
        path.unlink(missing_ok=True)


def _draft_from_run(run: RunnerTest) -> ConfigurationDraft:
    if isinstance(run.injection, RegisterInjection):
        injection = (
            f"register data_only: {'true' if run.injection.data_only else 'false'}"
        )
    elif isinstance(run.injection, MemoryInjection):
        injection = "memory"
    elif isinstance(run.injection, GlobalInjection):
        injection = "global"
    else:
        raise TypeError(f"Unsupported injection: {type(run.injection).__name__}")
    functions = ", ".join(
        function.name
        if not function.variables
        else (
            f"{function.name}("
            + ", ".join(
                ("@" if variable.is_global else "") + variable.name
                if variable.length is None
                else ("@" if variable.is_global else "") + f"{variable.name}:{variable.length}"
                for variable in function.variables
            )
            + ")"
        )
        for function in run.functions
    )
    return ConfigurationDraft(
        name=run.name,
        programs=list(run.programs),
        data_mode=[run.data_protection.name.lower()],
        control_mode=[run.control_flow_protection.name.lower().replace("_", "-")],
        timeout=str(run.timeout_ms),
        repetitions=str(run.execution_repetitions),
        measure_overhead=run.measure_overhead,
        measurement_repetitions=str(run.measurement_repetitions) if run.measurement_repetitions is not None else "",
        compiler_flags=", ".join(run.compiler_flags),
        injection=[injection],
        functions=functions,
    )


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
    lines = [f"TEST  {result['name']}", f"  per-process timeout: {result.get('timeout_ms', 'n/a')} ms", "", "OUTCOME", f"  return code     oracle {oracle['returncode']}   ASPIS {aspis['returncode']}",
             f"  stdout          {'same' if oracle['stdout'] == aspis['stdout'] else 'DIFFERENT'}",
             f"  stderr          {'same' if oracle['stderr'] == aspis['stderr'] else 'DIFFERENT'}", "", "MEASUREMENTS"]
    for field, unit in (("execution_time_seconds", "s"), ("binary_size_bytes", "B")):
        delta = difference[field]
        percentage = "n/a" if delta["percent"] is None else f"{delta['percent']:+.2f}%"
        lines.append(f"  {field}: oracle {oracle[field]:.6g} {unit} · ASPIS {aspis[field]:.6g} {unit} · Δ {delta['absolute']:+.6g} ({percentage})")
    lines.extend(["", "VARIABLES"])
    names = sorted(set(oracle.get("variables", {})) | set(aspis.get("variables", {})))
    lines.extend(f"  {'OK' if oracle.get('variables', {}).get(name) == aspis.get('variables', {}).get(name) else 'DIFF':4} {name}: {oracle.get('variables', {}).get(name, '<missing>')} → {aspis.get('variables', {}).get(name, '<missing>')}" for name in names)
    return lines


def _wrapped_lines(lines: Iterable[str], width: int) -> list[str]:
    width = max(1, width)
    return [line[index:index + width]
            for line in lines
            for index in range(0, max(1, len(line)), width)]


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
