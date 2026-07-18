from villani_ops.runners.villani_code import VillaniCodeRunner, VillaniCodeAdapter
from villani_ops.runners.base import UnsupportedRunnerAdapter


def runner_for_name(name: str):
    normalized = name.replace("_", "-")
    if normalized == "villani-code":
        return VillaniCodeAdapter()
    if normalized == "codex":
        from villani_ops.runners.codex_app_server import CodexAppServerRunner

        return CodexAppServerRunner()
    if normalized == "claude-code":
        from villani_ops.runners.claude_code import ClaudeCodeRunner

        return ClaudeCodeRunner()
    if normalized in {"pi", "aider"}:
        return UnsupportedRunnerAdapter(normalized)
    raise ValueError(
        f"Unsupported runner '{name}'. Supported runners: villani-code, codex, claude-code."
    )


__all__ = [
    "VillaniCodeRunner",
    "VillaniCodeAdapter",
    "runner_for_name",
]
