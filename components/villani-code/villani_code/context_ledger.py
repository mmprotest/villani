from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping


ContextStatus = Literal["active", "superseded", "compacted", "discarded"]


@dataclass(slots=True)
class ContextItem:
    item_id: str
    source_type: str
    source_reference: str
    content_digest: str
    repository_state_digest: str
    created_turn: int
    last_used_turn: int
    supersedes: list[str]
    relevance_score: float
    estimated_tokens: int
    status: ContextStatus
    content: str = field(repr=False, default="")
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    def durable_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("content", None)
        payload.pop("metadata", None)
        return payload


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def _content_digest(content: str) -> str:
    return hashlib.sha256(
        content.encode("utf-8", errors="replace")
    ).hexdigest()


def _tool_source_reference(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> tuple[str, str]:
    if tool_name == "Read":
        path = str(arguments.get("file_path", "")).replace("\\", "/")
        start = arguments.get("start_line")
        end = arguments.get("end_line")
        return "file_excerpt", f"{path}:{start or 1}-{end or 'eof'}"
    if tool_name == "GitDiff":
        return "candidate_patch", "git-diff"
    if tool_name == "Bash":
        return "command_output", str(arguments.get("command", ""))[:240]
    if tool_name in {"Grep", "Search", "FindSymbol", "FindReferences"}:
        query = (
            arguments.get("pattern")
            or arguments.get("query")
            or arguments.get("symbol")
            or ""
        )
        return "search_result", f"{tool_name}:{query}"
    return "tool_output", f"{tool_name}:{json.dumps(dict(arguments), sort_keys=True, default=str)}"


class ContextLedger:
    def __init__(
        self,
        *,
        repo: Path,
        task: str,
        max_projection_chars: int = 50_000,
    ):
        self.repo = repo.resolve()
        self.task = task
        self.max_projection_chars = max(1, int(max_projection_chars))
        self.items: list[ContextItem] = []
        self._by_tool_use_id: dict[str, str] = {}
        self._next_item_number = 1
        self.context_items_added = 0
        self.context_items_reused = 0
        self.context_items_compacted = 0
        self.tokens_removed_by_compaction = 0
        self.estimated_tokens_before_projection = 0
        self.estimated_tokens_after_projection = 0

    def _find_item(self, item_id: str) -> ContextItem | None:
        return next((item for item in self.items if item.item_id == item_id), None)

    def add(
        self,
        *,
        source_type: str,
        source_reference: str,
        content: str,
        repository_state_digest: str,
        turn: int,
        relevance_score: float = 0.5,
        summary: str = "",
        supersedes: list[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ContextItem:
        digest = _content_digest(content)
        for item in reversed(self.items):
            if (
                item.source_type == source_type
                and item.source_reference == source_reference
                and item.content_digest == digest
                and item.repository_state_digest == repository_state_digest
                and item.status != "discarded"
            ):
                item.last_used_turn = turn
                self.context_items_reused += 1
                return item

        superseded = list(supersedes or [])
        if source_type in {"candidate_patch", "validation"}:
            for item in self.items:
                if item.source_type == source_type and item.status == "active":
                    item.status = "superseded"
                    superseded.append(item.item_id)
        if source_type == "file_excerpt":
            path = source_reference.split(":", 1)[0]
            for item in self.items:
                prior_path = item.source_reference.split(":", 1)[0]
                if (
                    item.source_type == "file_excerpt"
                    and prior_path == path
                    and item.status == "active"
                ):
                    if item.repository_state_digest != repository_state_digest:
                        item.status = "superseded"
                        superseded.append(item.item_id)
                    elif (
                        item.source_reference.endswith(":1-eof")
                        and source_reference != item.source_reference
                    ):
                        item.status = "compacted"

        item = ContextItem(
            item_id=f"context-item-{self._next_item_number:04d}",
            source_type=source_type,
            source_reference=source_reference,
            content_digest=digest,
            repository_state_digest=repository_state_digest,
            created_turn=turn,
            last_used_turn=turn,
            supersedes=sorted(dict.fromkeys(superseded)),
            relevance_score=max(0.0, min(1.0, float(relevance_score))),
            estimated_tokens=estimate_tokens(content),
            status="active",
            content=content,
            summary=(
                summary
                or (content.splitlines()[0][:180] if content else "")
            ),
            metadata=dict(metadata or {}),
        )
        self._next_item_number += 1
        self.items.append(item)
        self.context_items_added += 1
        return item

    def record_tool_result(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        tool_use_id: str,
        content: str,
        repository_state_digest: str,
        turn: int,
        result_id: str | None = None,
        duplicate_of: str | None = None,
    ) -> ContextItem | None:
        if duplicate_of:
            self.context_items_reused += 1
            return None
        source_type, source_reference = _tool_source_reference(
            tool_name,
            arguments,
        )
        if source_type == "file_excerpt":
            summary = (
                f"Read evidence for {source_reference}; reopen with Read "
                "if the full payload is needed."
            )
        elif source_type == "search_result":
            summary = (
                f"Search evidence for {source_reference}; rerun with "
                "refresh=true only if fresh output is required."
            )
        elif source_type == "command_output":
            summary = (
                f"Command evidence for {source_reference}; full first "
                "output remains in durable debug evidence."
            )
        else:
            summary = f"Tool evidence for {source_reference}."
        item = self.add(
            source_type=source_type,
            source_reference=source_reference,
            content=content,
            repository_state_digest=repository_state_digest,
            turn=turn,
            relevance_score=0.9
            if source_type in {"candidate_patch", "validation", "file_excerpt"}
            else 0.6,
            summary=summary,
            metadata={"result_id": result_id} if result_id else {},
        )
        self._by_tool_use_id[tool_use_id] = item.item_id
        return item

    def record_validation(
        self,
        *,
        content: str,
        repository_state_digest: str,
        turn: int,
    ) -> ContextItem:
        return self.add(
            source_type="validation",
            source_reference="latest-validation",
            content=content,
            repository_state_digest=repository_state_digest,
            turn=turn,
            relevance_score=1.0,
        )

    def _compact_block(
        self,
        block: dict[str, Any],
        item: ContextItem,
    ) -> dict[str, Any]:
        old_content = str(block.get("content", ""))
        reference = (
            f"[context reference {item.item_id}; status={item.status}; "
            f"digest={item.content_digest[:12]}] "
            f"{item.summary[:180]}"
        )
        removed = max(0, estimate_tokens(old_content) - estimate_tokens(reference))
        self.tokens_removed_by_compaction += removed
        self.context_items_compacted += 1
        return {**block, "content": reference}

    def project_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        projected = copy.deepcopy(messages)
        before_chars = sum(len(str(message.get("content", ""))) for message in projected)
        self.estimated_tokens_before_projection = estimate_tokens(
            "".join(str(message.get("content", "")) for message in projected)
        )

        for message in projected:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            new_blocks: list[Any] = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    new_blocks.append(block)
                    continue
                tool_use_id = str(block.get("tool_use_id", ""))
                item_id = self._by_tool_use_id.get(tool_use_id)
                item = self._find_item(item_id) if item_id else None
                if item is not None and item.status != "active":
                    new_blocks.append(self._compact_block(block, item))
                else:
                    new_blocks.append(block)
            message["content"] = new_blocks

        projected_chars = sum(
            len(str(message.get("content", ""))) for message in projected
        )
        if projected_chars > self.max_projection_chars:
            candidates = sorted(
                (
                    item
                    for item in self.items
                    if item.status == "active"
                    and item.source_type
                    not in {"candidate_patch", "validation"}
                ),
                key=lambda item: (
                    item.relevance_score,
                    item.last_used_turn,
                    item.created_turn,
                ),
            )
            for item in candidates:
                if projected_chars <= self.max_projection_chars:
                    break
                item.status = "compacted"
                for message in projected:
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for index, block in enumerate(content):
                        if not isinstance(block, dict):
                            continue
                        tool_use_id = str(block.get("tool_use_id", ""))
                        if self._by_tool_use_id.get(tool_use_id) != item.item_id:
                            continue
                        replacement = self._compact_block(block, item)
                        projected_chars -= max(
                            0,
                            len(str(block.get("content", "")))
                            - len(str(replacement.get("content", ""))),
                        )
                        content[index] = replacement

        if projected_chars > self.max_projection_chars:
            for message_index, message in enumerate(projected[:-4]):
                if projected_chars <= self.max_projection_chars:
                    break
                if message_index == 0 or message.get("role") != "assistant":
                    continue
                content = message.get("content")
                if not isinstance(content, list) or any(
                    isinstance(block, dict) and block.get("type") == "tool_use"
                    for block in content
                ):
                    continue
                replacement_blocks: list[Any] = []
                changed = False
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        old_text = str(block.get("text", ""))
                        compact_text = "[compacted prior model prose]"
                        projected_chars -= max(
                            0,
                            len(old_text) - len(compact_text),
                        )
                        self.tokens_removed_by_compaction += max(
                            0,
                            estimate_tokens(old_text)
                            - estimate_tokens(compact_text),
                        )
                        replacement_blocks.append(
                            {**block, "text": compact_text}
                        )
                        changed = True
                    else:
                        replacement_blocks.append(block)
                if changed:
                    self.context_items_compacted += 1
                    message["content"] = replacement_blocks

        after_chars = sum(len(str(message.get("content", ""))) for message in projected)
        self.estimated_tokens_after_projection = estimate_tokens(
            "".join(str(message.get("content", "")) for message in projected)
        )
        if after_chars > before_chars:
            self.estimated_tokens_after_projection = min(
                self.estimated_tokens_after_projection,
                self.estimated_tokens_before_projection,
            )
        return projected

    def active_items(self) -> list[ContextItem]:
        return [item for item in self.items if item.status == "active"]

    def telemetry(self) -> dict[str, Any]:
        return {
            "context_items_added": self.context_items_added,
            "context_items_reused": self.context_items_reused,
            "context_items_compacted": self.context_items_compacted,
            "tokens_removed_by_compaction": self.tokens_removed_by_compaction,
            "estimated_tokens_before_projection": (
                self.estimated_tokens_before_projection
            ),
            "estimated_tokens_after_projection": (
                self.estimated_tokens_after_projection
            ),
        }
