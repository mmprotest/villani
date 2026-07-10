from __future__ import annotations

import random
import secrets
import time

from rich.text import Text
from textual.widgets import Static

from villani_code.tui.assets import SpinnerTheme, spinner_themes


class SpinnerWidget(Static):
    def __init__(self) -> None:
        super().__init__(Text("[*] Idle"), id="spinner")
        self._themes = spinner_themes()
        self._rng = random.Random(secrets.randbits(64) ^ time.time_ns())
        self._theme: SpinnerTheme = self._themes[0]
        self._frame = 0
        self._active = False
        self._label = "Idle"

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)

    def set_state(self, active: bool, label: str | None = None) -> None:
        self._active = active
        if active:
            self._theme = self._rng.choice(self._themes)
            self._label = label or self._rng.choice(self._theme.slogans)
            self._frame = 0
        elif label:
            self._label = label
        self._render_now()

    def _tick(self) -> None:
        if self._active:
            self._frame += 1
            self._render_now()

    def _render_text(self) -> Text:
        if self._active:
            frame = self._theme.frames[self._frame % len(self._theme.frames)]
            return Text(f"[{frame}] {self._label}")
        return Text(f"[*] {self._label}")

    def _render_now(self) -> None:
        self.update(self._render_text(), layout=False)
