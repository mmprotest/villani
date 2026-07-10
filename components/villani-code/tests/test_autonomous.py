from __future__ import annotations

from pathlib import Path

from villani_code.autonomy import Opportunity, TakeoverConfig, TaskContract
from villani_code.autonomous import VillaniModeController


class SequencedRunner:
    def __init__(self, repo: Path, steps: list[dict]) -> None:
        self.repo = repo
        self.steps = steps
        self.index = 0

    def run(self, _prompt: str, execution_budget=None):
        step = self.steps[min(self.index, len(self.steps) - 1)]
        self.index += 1
        for rel, content in step.get("writes", []):
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return {
            "response": {"content": [{"type": "text", "text": step.get("text", "done")} ]},
            "transcript": {"tool_results": step.get("tool_results", [])},
            "execution": {
                "turns_used": 1,
                "tool_calls_used": 0,
                "elapsed_seconds": 0.01,
                "terminated_reason": step.get("terminated_reason", "completed"),
                "intentional_changes": step.get("intentional_changes", []),
                "validation_artifacts": step.get("validation_artifacts", ["pytest -q tests/test_runner_defaults.py (exit=0)"]),
                "runner_failures": step.get("runner_failures", []),
                "inspection_summary": step.get("inspection_summary", "ok"),
            },
        }


class SequencedPlanner:
    def __init__(self, waves: list[list[Opportunity]]) -> None:
        self.waves = waves
        self.calls = 0

    def build_repo_summary(self) -> str:
        return "summary"

    def discover_opportunities(self) -> list[Opportunity]:
        idx = min(self.calls, len(self.waves) - 1)
        self.calls += 1
        return self.waves[idx]


def _op(title: str, category: str = "test", blast_radius: str = "repo") -> Opportunity:
    return Opportunity(
        title=title,
        category=category,
        priority=0.9,
        confidence=0.9,
        affected_files=["villani_code/state.py", "pyproject.toml"],
        evidence="e",
        blast_radius=blast_radius,
        proposed_next_action="act",
        task_contract=TaskContract.EFFECTFUL.value,
    )


def test_villani_processes_multiple_interventions_in_single_run(tmp_path: Path) -> None:
    runner = SequencedRunner(
        tmp_path,
        [
            {"intentional_changes": ["villani_code/a.py"]},
            {"intentional_changes": ["villani_code/b.py"]},
        ],
    )
    planner = SequencedPlanner([[ _op("Task A") ], [ _op("Task B") ], []])
    controller = VillaniModeController(runner, tmp_path, takeover_config=TakeoverConfig(max_waves=5, max_total_task_attempts=0))
    controller.planner = planner

    summary = controller.run()

    assert len(summary["tasks_attempted"]) >= 2
    assert planner.calls >= 2


def test_villani_stops_on_stagnation(tmp_path: Path) -> None:
    runner = SequencedRunner(tmp_path, [{"terminated_reason": "model_idle", "inspection_summary": ""}] * 5)
    planner = SequencedPlanner([[ _op("Task A") ]] * 6)
    controller = VillaniModeController(
        runner,
        tmp_path,
        takeover_config=TakeoverConfig(stagnation_cycle_limit=2, max_total_task_attempts=0, max_waves=5),
    )
    controller.planner = planner

    summary = controller.run()

    assert "No meaningful progress" in summary["done_reason"]


def test_villani_respects_global_attempt_budget_when_configured(tmp_path: Path) -> None:
    runner = SequencedRunner(tmp_path, [{"intentional_changes": ["villani_code/a.py"]}] * 5)
    planner = SequencedPlanner([[ _op("Task A") ]] * 8)
    controller = VillaniModeController(
        runner,
        tmp_path,
        takeover_config=TakeoverConfig(max_total_task_attempts=2, max_waves=5),
    )
    controller.planner = planner

    summary = controller.run()

    assert summary["done_reason"] == "Villani mode budget exhausted."
