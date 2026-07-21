"""Stable prompt construction for one isolated Claude Code coding attempt."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


CLAUDE_CODING_PROMPT_VERSION = "villani.claude_code_coding_prompt.v1"


@dataclass(frozen=True, slots=True)
class ClaudeCodingPrompt:
    version: str
    text: str
    sha256: str

    @property
    def bytes(self) -> bytes:
        return self.text.encode("utf-8")


def build_claude_coding_prompt(
    *,
    task: str,
    success_criteria: str,
    attempt_id: str,
    worktree: Path,
    instruction_policy: str,
) -> ClaudeCodingPrompt:
    resolved = Path(worktree).resolve()
    text = (
        f"Prompt contract: {CLAUDE_CODING_PROMPT_VERSION}\n"
        f"Candidate attempt identifier: {attempt_id}\n"
        f"Instruction policy: {instruction_policy}\n"
        f"Exact writable worktree scope: {resolved}\n\n"
        "<villani-task>\n"
        f"{task}\n"
        "</villani-task>\n\n"
        "<villani-success-criteria>\n"
        f"{success_criteria}\n"
        "</villani-success-criteria>\n\n"
        "Own the complete coding loop for this candidate. Inspect and modify only "
        "the worktree named above. Run relevant validation in that worktree. Do "
        "not commit, push, open a pull request, start a cloud session, or mutate "
        "any external repository. Do not claim success without checking your work. "
        "Finish with the exact structured summary required by the supplied JSON "
        "Schema; that summary is supplementary and Villani derives patch truth "
        "from Git.\n"
    )
    digest = f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
    return ClaudeCodingPrompt(CLAUDE_CODING_PROMPT_VERSION, text, digest)


__all__ = [
    "CLAUDE_CODING_PROMPT_VERSION",
    "ClaudeCodingPrompt",
    "build_claude_coding_prompt",
]
