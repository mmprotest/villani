from __future__ import annotations

import pytest
import typer

from villani_code import cli
from villani_code.interrupts import InterruptController


def test_interrupt_controller_first_interrupt_then_exit_and_reset() -> None:
    controller = InterruptController()
    assert controller.register_interrupt() == "interrupt"
    assert controller.register_interrupt() == "exit"
    controller.reset_interrupt_state()
    assert controller.register_interrupt() == "interrupt"


def test_run_interactive_exits_on_second_ctrl_c(monkeypatch, tmp_path) -> None:
    class FakeShell:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0

        def run(self):
            self.calls += 1
            raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_build_runner", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_load_interactive_shell", lambda: (FakeShell, RuntimeError))

    with pytest.raises(typer.Exit) as exc:
        cli._run_interactive("u", "m", tmp_path, 100, False, "anthropic", None)

    assert exc.value.exit_code == 130


def test_run_interactive_resets_interrupt_state_after_idle(monkeypatch, tmp_path) -> None:
    class FakeShell:
        run_calls = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def run(self):
            type(self).run_calls += 1
            if type(self).run_calls == 1:
                raise KeyboardInterrupt()
            return None

    monkeypatch.setattr(cli, "_build_runner", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_load_interactive_shell", lambda: (FakeShell, RuntimeError))

    cli._run_interactive("u", "m", tmp_path, 100, False, "anthropic", None)

    assert FakeShell.run_calls == 2
