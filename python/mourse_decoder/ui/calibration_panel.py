"""Calibration page: measure the user's own key rhythm and suggest the timing values."""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable

from .. import calibration
from ..calibration import DASH, DOT, LETTER, WORD
from ..controller import MorseController
from ..i18n import tr
from ..settings import Settings
from . import theme
from .theme import Button, Card, label

log = logging.getLogger(__name__)

# (result row, settings field it would change)
RESULTS = (
    ("threshold", "dot_threshold_ms"),
    ("letter", "letter_gap_ms"),
    ("word", "word_gap_ms"),
)


class CalibrationPanel(tk.Frame):
    def __init__(self, master: tk.Misc, controller: MorseController, apply_settings: Callable[[Settings], None]) -> None:
        super().__init__(master, bg=theme.P.bg)
        self._controller = controller
        self._apply_settings = apply_settings
        self._session = calibration.Session(use_threshold=not controller.settings.separate_dash_key)
        self._measuring = False
        self.columnconfigure(0, weight=1)

        tk.Label(
            self, text=tr("calibration.intro"), bg=theme.P.bg, fg=theme.P.muted, font=theme.font(10), justify="left",
            anchor="w", wraplength=640,
        ).grid(row=0, column=0, sticky="w")

        self._state = tk.StringVar()
        tk.Label(
            self, textvariable=self._state, bg=theme.P.bg, fg=theme.P.text, font=theme.font(11, "bold"), justify="left",
            anchor="w", wraplength=640,
        ).grid(row=1, column=0, sticky="w", pady=(16, 0))
        self._hint = tk.StringVar()
        tk.Label(
            self, textvariable=self._hint, bg=theme.P.bg, fg=theme.P.muted, font=theme.font(10), justify="left",
            anchor="w", wraplength=640,
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))

        steps = Card(self)
        steps.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        self._progress: dict[str, tk.StringVar] = {}
        for phase in calibration.PHASES:
            var = tk.StringVar()
            label(steps.add_row(tr(f"calibration.step.{phase}")), "", weight="bold", textvariable=var).pack()
            self._progress[phase] = var

        label(self, tr("calibration.result.title"), size=9, weight="bold", muted=True).grid(
            row=4, column=0, sticky="w", pady=(16, 8)
        )
        results = Card(self)
        results.grid(row=5, column=0, sticky="ew")
        self._results: dict[str, tk.StringVar] = {}
        for key, _field in RESULTS:
            var = tk.StringVar()
            label(results.add_row(tr(f"calibration.result.{key}")), "", weight="bold", textvariable=var).pack()
            self._results[key] = var

        self._notes = tk.StringVar()
        tk.Label(
            self, textvariable=self._notes, bg=theme.P.bg, fg=theme.P.muted, font=theme.font(9), justify="left",
            anchor="w", wraplength=640,
        ).grid(row=6, column=0, sticky="w", pady=(8, 0))

        buttons = tk.Frame(self, bg=theme.P.bg)
        buttons.grid(row=7, column=0, sticky="w", pady=(8, 0))
        self._start_btn = Button(buttons, tr("calibration.start"), self._toggle, variant="primary", min_width=140)
        self._next_btn = Button(buttons, tr("calibration.next"), self._next_step, min_width=140)
        self._restart_btn = Button(buttons, tr("calibration.restart"), self._restart_step, min_width=140)
        self._apply_btn = Button(buttons, tr("calibration.apply"), self._apply, min_width=140)
        for button in (self._start_btn, self._next_btn, self._restart_btn, self._apply_btn):
            button.pack(side="left", padx=(0, 8))

        self.bind("<Destroy>", self._destroyed)
        self.refresh()

    @property
    def measuring(self) -> bool:
        return self._measuring

    # called on the UI thread via TrayApp.call_in_ui
    def add_timing(self, kind: str, ms: int) -> None:
        if not self._measuring:
            return
        if kind == "press_ms":
            self._session.add_press(ms)
        elif kind == "gap_ms":
            self._session.add_gap(ms)
        if self._session.done:
            self.stop()
        self._show()

    def stop(self) -> None:
        """End the measuring (the results stay). Also called when the page is left."""
        if self._measuring:
            self._measuring = False
            self._controller.set_calibrating(False)
            self._show()

    def refresh(self) -> None:
        """Settings changed elsewhere: show the current values and a matching measurement."""
        # the steps depend on the key mode; a running measurement is not swapped under the user
        if not self._measuring and (DOT in self._session.phases) == self._controller.settings.separate_dash_key:
            self._new_session()
        self._show()

    def _destroyed(self, event: tk.Event) -> None:
        if event.widget is self and self._measuring:
            self._measuring = False
            self._controller.set_calibrating(False)

    def _new_session(self) -> None:
        self._session = calibration.Session(use_threshold=not self._controller.settings.separate_dash_key)

    def _toggle(self) -> None:
        if self._measuring:
            self.stop()
            return
        if self._session.done or not any(self._session.samples(p) for p in calibration.PHASES):
            self._new_session()
        self._measuring = True
        self._controller.set_calibrating(True)
        self._show()

    def _next_step(self) -> None:
        self._session.next_step()
        if self._session.done:
            self.stop()
        self._show()

    def _restart_step(self) -> None:
        self._session.restart_step()
        self._show()

    def _apply(self) -> None:
        proposal = self._session.proposal()
        if not proposal.usable:
            self._notes.set(tr("calibration.nothing"))
            return
        try:
            new = calibration.apply_proposal(self._controller.settings, proposal)
            self._apply_settings(new)
        except Exception as exc:  # noqa: BLE001 - the settings page shows every refusal the same way
            if not isinstance(exc, ValueError):
                log.exception("Kalibrierung: Werte nicht übernommen")
            self._notes.set(tr("calibration.apply_failed", msg=str(exc)))
            return
        self._notes.set(tr("calibration.applied"))
        self._show(keep_notes=True)

    def _show(self, keep_notes: bool = False) -> None:
        session, settings = self._session, self._controller.settings
        phase = session.phase
        if self._measuring and phase:
            self._state.set(tr("calibration.state.running", step=tr(f"calibration.step.{phase}")))
            self._hint.set(tr(f"calibration.hint.{phase}", n=session.target))
        elif session.done:
            self._state.set(tr("calibration.state.done"))
            self._hint.set("")
        else:
            self._state.set(tr("calibration.state.idle"))
            self._hint.set(tr("calibration.separate_note") if settings.separate_dash_key else "")
        self._start_btn.set_text(tr("calibration.stop") if self._measuring else tr("calibration.start"))
        self._next_btn.set_enabled(self._measuring)
        self._restart_btn.set_enabled(self._measuring)

        proposal = session.proposal()
        groups = {DOT: proposal.dot, DASH: proposal.dash, LETTER: proposal.letter, WORD: proposal.word}
        for name in calibration.PHASES:
            samples = session.samples(name)
            total = calibration.TARGETS[name]
            text = f"{len(samples)} / {total}"
            if name not in session.phases:
                text = "–"
            elif groups[name].median is not None:
                text += f" · {groups[name].median} ms"
            self._progress[name].set(text)

        suggested = {
            "threshold": proposal.dot_threshold_ms,
            "letter": proposal.letter_gap_ms,
            "word": proposal.word_gap_ms,
        }
        for key, field in RESULTS:
            value = suggested[key]
            self._results[key].set(
                tr(
                    "calibration.result.value",
                    now=getattr(settings, field),
                    suggested=f"{value} ms" if value is not None else tr("calibration.result.none"),
                )
            )
        if not keep_notes:
            parts = [tr(f"calibration.issue.{code}") for code in proposal.issues]
            dropped = sum(g.dropped for g in groups.values())
            if dropped:
                parts.append(tr("calibration.dropped", n=dropped))
            self._notes.set(" ".join(parts))
