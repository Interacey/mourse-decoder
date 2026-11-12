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
        self._shown_measuring = False  # what the Start/Stop button shows, so a click means what it says
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
        self.bind("<Unmap>", self._unmapped)  # the page is hidden (tab left, window minimized or withdrawn)
        self.refresh()

    @property
    def measuring(self) -> bool:
        """The controller's state: page and controller cannot disagree about it."""
        return self._controller.calibrating

    # called on the UI thread via TrayApp.call_in_ui
    def add_timing(self, kind: str, ms: int) -> None:
        if not self.measuring:
            return
        try:
            if kind == "press_ms":
                self._session.add_press(ms)
            elif kind == "gap_ms":
                self._session.add_gap(ms)
            if self._session.done:
                self.stop()
            self._show()
        except Exception:
            # a failing page must not leave the engine measuring (letters would stay muted)
            log.exception("Kalibrierung: Messwert nicht verarbeitet")
            self._release()

    def stop(self) -> None:
        """End the measuring (the results stay). Also called when the page is left.

        Always passed on to the controller (switching off is idempotent there), whatever the page thinks.
        """
        try:
            self._release()
        finally:
            self._show()

    def controller_ended(self, reason: str) -> None:
        """The controller ended the measuring itself (stop, time limit): show the state, keep the results.

        The notice can be late (it waits in the UI queue); if a new measurement runs by now it is stale and
        only the display is refreshed.
        """
        self._show()
        if reason == "timeout" and not self.measuring:
            self._notes.set(tr("calibration.timeout"))

    def refresh(self) -> None:
        """Settings changed elsewhere: show the current values and a matching measurement."""
        # the steps depend on the key mode: measuring stops, the new mode starts a matching session
        if (DOT in self._session.phases) == self._controller.settings.separate_dash_key:
            self._release()
            self._new_session()
        self._show()

    def _release(self) -> None:
        """Measuring off in the controller; never raises."""
        try:
            self._controller.set_calibrating(False)
        except Exception:  # noqa: BLE001 - there is nothing more the page can do
            log.exception("Kalibrierung: Messen konnte nicht beendet werden")

    def _destroyed(self, event: tk.Event) -> None:
        if event.widget is self:
            self._release()

    def _unmapped(self, event: tk.Event) -> None:
        if event.widget is self:
            self.stop()

    def _proposal(self) -> calibration.Proposal:
        settings = self._controller.settings
        return self._session.proposal(
            word_was_off=settings.word_gap_ms == 0,
            word_gap_now=settings.word_gap_ms,
            letter_gap_now=settings.letter_gap_ms,
        )

    def _new_session(self) -> None:
        self._session = calibration.Session(use_threshold=not self._controller.settings.separate_dash_key)

    def _toggle(self) -> None:
        if self._shown_measuring:  # the button says "Stop", even if the controller ended meanwhile
            self.stop()
            return
        if self._session.done or not any(self._session.samples(p) for p in calibration.PHASES):
            self._new_session()
        self._session.resume()  # starting resets the engine: the next press has no pause before it
        if not self._controller.set_calibrating(True):
            self._show()
            self._notes.set(tr("calibration.not_running"))
            return
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
        self.stop()  # new values and a running measurement do not mix
        proposal = self._proposal()
        if not proposal.usable:
            self._notes.set(tr("calibration.nothing"))
            return
        try:
            new = calibration.apply_proposal(self._controller.settings, proposal)
            self._apply_settings(new)
        except Exception as exc:  # noqa: BLE001 - the settings page shows every refusal the same way
            # the text of Settings.validate is German only: log it, show the localized sentence
            log.warning("Kalibrierung: Werte nicht übernommen: %s", exc, exc_info=not isinstance(exc, ValueError))
            self._notes.set(tr("calibration.apply.failed"))
            return
        self._notes.set(tr("calibration.applied"))
        self._show(keep_notes=True)

    def _show(self, keep_notes: bool = False) -> None:
        session, settings = self._session, self._controller.settings
        phase = session.phase
        measuring = self._shown_measuring = self.measuring
        if measuring and phase:
            self._state.set(tr("calibration.state.running", step=tr(f"calibration.step.{phase}")))
            hint = tr(f"calibration.hint.{phase}", n=session.target)
            if phase in (DOT, DASH):
                hint += " " + tr("calibration.hint.rhythm")
            self._hint.set(hint)
        elif session.done:
            self._state.set(tr("calibration.state.done"))
            self._hint.set("")
        else:
            self._state.set(tr("calibration.state.idle"))
            self._hint.set(tr("calibration.separate_note") if settings.separate_dash_key else "")
        self._start_btn.set_text(tr("calibration.stop") if measuring else tr("calibration.start"))
        self._next_btn.set_enabled(measuring)
        self._restart_btn.set_enabled(measuring)

        proposal = self._proposal()
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
            parts = [tr(f"calibration.issue.{code}", **proposal.details.get(code, {})) for code in proposal.issues]
            dropped = sum(g.dropped for g in groups.values())
            if dropped:
                parts.append(tr("calibration.dropped", n=dropped))
            if session.ignored:
                parts.append(tr("calibration.ignored", n=session.ignored))
            if not settings.enabled:
                parts.append(tr("calibration.disabled_note"))
            self._notes.set(" ".join(parts))
