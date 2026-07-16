from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

from villani_code.repository_state import file_sha256


class PatchApplyError(Exception):
    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.details = details or {}


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]


@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[Hunk]


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_METADATA_PREFIXES = (
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "Binary files ",
    "GIT binary patch",
)


@dataclass
class PatchApplyDiagnostics:
    fallback_files: list[str]




def extract_unified_diff_targets(diff_text: str, default_file_path: str | None = None) -> list[str]:
    patches = parse_unified_diff(diff_text)
    if not patches:
        if default_file_path:
            return [_normalize_patch_path(default_file_path)]
        return []
    targets: list[str] = []
    for file_patch in patches:
        targets.append(_resolve_target_path(file_patch, default_file_path))
    return targets

def apply_unified_diff(repo: Path, diff_text: str, default_file_path: str | None = None) -> list[str]:
    touched, _diagnostics = apply_unified_diff_with_diagnostics(repo, diff_text, default_file_path=default_file_path)
    return touched


def apply_unified_diff_with_diagnostics(
    repo: Path,
    diff_text: str,
    default_file_path: str | None = None,
    expected_file_digests: Mapping[str, str] | None = None,
) -> tuple[list[str], PatchApplyDiagnostics]:
    patches = parse_unified_diff(diff_text)
    if not patches:
        if default_file_path:
            raise PatchApplyError(f"No file patch found in unified diff for {default_file_path}")
        raise PatchApplyError("No file patch found in unified diff")

    diagnostics = PatchApplyDiagnostics(fallback_files=[])
    expected_digests = {
        _normalize_relative_patch_path(str(path)): str(digest)
        for path, digest in (expected_file_digests or {}).items()
        if str(path).strip() and str(digest).strip()
    }
    targets: list[tuple[Path, str | None]] = []
    for fp in patches:
        rel = _normalize_relative_patch_path(
            _resolve_target_path(fp, default_file_path)
        )
        path = (repo / rel).resolve()
        try:
            path.relative_to(repo.resolve())
        except ValueError as exc:
            raise PatchApplyError(
                f"{rel}: patch target escapes repository",
                details=_failure_details(
                    rel=rel,
                    path=path,
                    expected_digest=expected_digests.get(rel),
                    actual_digest=file_sha256(path),
                    hunk=fp.hunks[0] if fp.hunks else None,
                    retry_guidance="Regenerate the patch with a repository-relative target path.",
                ),
            ) from exc
        expected_digest = expected_digests.get(rel)
        actual_digest = file_sha256(path)
        if expected_digest is not None and actual_digest != expected_digest:
            raise PatchApplyError(
                f"{rel}: stale preimage digest; expected {expected_digest}, got {actual_digest}",
                details=_failure_details(
                    rel=rel,
                    path=path,
                    expected_digest=expected_digest,
                    actual_digest=actual_digest,
                    hunk=fp.hunks[0] if fp.hunks else None,
                    retry_guidance=(
                        "Re-read the current file range, use its new content_sha256, "
                        "and regenerate only the failed hunk."
                    ),
                ),
            )
        old_content = None
        if path.exists():
            old_content = path.read_bytes().decode("utf-8", errors="surrogateescape")
        newline_style = _detect_newline_style(old_content)
        try:
            new_content = _apply_file_patch(old_content, fp, rel, newline_style=newline_style)
        except PatchApplyError as exc:
            try:
                new_content = _apply_file_patch_with_fallback(
                    old_content,
                    fp,
                    rel,
                    newline_style=newline_style,
                    exact_error=exc,
                )
            except PatchApplyError as fallback_error:
                hunk = _failed_hunk(fp, str(fallback_error))
                raise PatchApplyError(
                    str(fallback_error),
                    details=_failure_details(
                        rel=rel,
                        path=path,
                        expected_digest=expected_digest,
                        actual_digest=actual_digest,
                        hunk=hunk,
                        retry_guidance=(
                            "Re-read the nearest context shown here, then regenerate "
                            "a narrow patch against the current preimage digest."
                        ),
                    ),
                ) from fallback_error
            diagnostics.fallback_files.append(rel)
        targets.append((path, new_content))

    # apply atomically after validation
    touched: list[str] = []
    for path, content in targets:
        if content is None:
            if path.exists():
                path.unlink()
                touched.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        touched.append(str(path))
    return touched, diagnostics


def _failed_hunk(file_patch: FilePatch, error: str) -> Hunk | None:
    match = re.search(r"hunk #(\d+)", error)
    if match:
        index = int(match.group(1)) - 1
        if 0 <= index < len(file_patch.hunks):
            return file_patch.hunks[index]
    return file_patch.hunks[0] if file_patch.hunks else None


def _serialize_hunk(hunk: Hunk | None) -> dict[str, Any] | None:
    if hunk is None:
        return None
    return {
        "header": (
            f"@@ -{hunk.old_start},{hunk.old_count} "
            f"+{hunk.new_start},{hunk.new_count} @@"
        ),
        "old_start": hunk.old_start,
        "old_count": hunk.old_count,
        "new_start": hunk.new_start,
        "new_count": hunk.new_count,
        "lines": list(hunk.lines),
    }


def _nearest_context(path: Path, hunk: Hunk | None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    lines = path.read_text(
        encoding="utf-8",
        errors="surrogateescape",
    ).splitlines()
    center = max((hunk.old_start if hunk is not None else 1) - 1, 0)
    start = max(0, center - 3)
    end = min(len(lines), center + max(hunk.old_count if hunk else 1, 1) + 3)
    return [
        {"line": index + 1, "text": lines[index]}
        for index in range(start, end)
    ]


def _failure_details(
    *,
    rel: str,
    path: Path,
    expected_digest: str | None,
    actual_digest: str,
    hunk: Hunk | None,
    retry_guidance: str,
) -> dict[str, Any]:
    return {
        "file": rel,
        "expected_digest": expected_digest,
        "actual_digest": actual_digest,
        "failed_hunk": _serialize_hunk(hunk),
        "nearest_context": _nearest_context(path, hunk),
        "retry_guidance": retry_guidance,
    }


def parse_unified_diff(diff_text: str) -> list[FilePatch]:
    text = diff_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    patches: list[FilePatch] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        diff_git_match = _DIFF_GIT_RE.match(line)
        if diff_git_match:
            i += 1
            while i < len(lines) and lines[i].startswith(_METADATA_PREFIXES):
                i += 1
            line = lines[i] if i < len(lines) else ""
        if not line.startswith("--- "):
            i += 1
            continue
        old_path = _normalize_patch_path(line[4:].strip().split("\t")[0])
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise PatchApplyError(f"Malformed diff near file header line {i}: missing +++ after {line!r}")
        new_path = _normalize_patch_path(lines[i][4:].strip().split("\t")[0])
        i += 1
        hunks: list[Hunk] = []
        while i < len(lines):
            cur = lines[i]
            if cur.startswith("--- ") or _DIFF_GIT_RE.match(cur):
                break
            if cur.startswith(_METADATA_PREFIXES) or not cur:
                i += 1
                continue
            m = _HUNK_RE.match(cur)
            if not m:
                if hunks:
                    break
                raise PatchApplyError(f"{new_path}: malformed hunk header at diff line {i + 1}: {cur!r}")
            old_start = int(m.group(1))
            old_count = int(m.group(2) or "1")
            new_start = int(m.group(3))
            new_count = int(m.group(4) or "1")
            i += 1
            hunk_lines: list[str] = []
            while i < len(lines):
                hl = lines[i]
                if _HUNK_RE.match(hl) or hl.startswith("--- ") or _DIFF_GIT_RE.match(hl):
                    break
                if hl.startswith((" ", "+", "-", "\\")):
                    hunk_lines.append(hl)
                    i += 1
                    continue
                if hl.startswith(_METADATA_PREFIXES):
                    break
                if hl:
                    break
                i += 1
            hunks.append(
                Hunk(old_start=old_start, old_count=old_count, new_start=new_start, new_count=new_count, lines=hunk_lines)
            )
        patches.append(FilePatch(old_path=old_path, new_path=new_path, hunks=hunks))
    return patches


def _normalize_patch_path(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _normalize_relative_patch_path(path: str) -> str:
    normalized = _normalize_patch_path(path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _resolve_target_path(file_patch: FilePatch, default_file_path: str | None) -> str:
    if file_patch.new_path != "/dev/null":
        return file_patch.new_path
    if file_patch.old_path != "/dev/null":
        return file_patch.old_path
    if default_file_path:
        return default_file_path
    raise PatchApplyError("Unable to determine target file path for patch")


def _apply_file_patch(
    existing: str | None, file_patch: FilePatch, display_path: str, *, newline_style: str
) -> str | None:
    if file_patch.new_path == "/dev/null":
        # delete file
        if existing is None:
            raise PatchApplyError(f"{display_path}: cannot delete missing file")
        return None

    current = existing if existing is not None else ""
    src_lines = current.splitlines(keepends=True)
    out_lines: list[str] = []
    src_idx = 0

    for h_idx, hunk in enumerate(file_patch.hunks, start=1):
        target_idx = max(hunk.old_start - 1, 0)
        if target_idx < src_idx:
            raise PatchApplyError(f"{display_path}: overlapping hunk #{h_idx}")
        out_lines.extend(src_lines[src_idx:target_idx])
        src_idx = target_idx
        for lno, hline in enumerate(hunk.lines, start=1):
            if not hline:
                continue
            tag = hline[0]
            payload = hline[1:]
            if tag == "\\":
                continue
            if tag == " ":
                _expect_line(src_lines, src_idx, payload, display_path, h_idx, lno, "context")
                out_lines.append(src_lines[src_idx])
                src_idx += 1
            elif tag == "-":
                _expect_line(src_lines, src_idx, payload, display_path, h_idx, lno, "remove")
                src_idx += 1
            elif tag == "+":
                out_lines.append(payload + newline_style)
            else:
                raise PatchApplyError(f"{display_path}: unsupported hunk line tag {tag!r} in hunk #{h_idx}")
            if tag == "+" and _next_line_has_no_newline_marker(hunk.lines, lno - 1) and out_lines:
                out_lines[-1] = out_lines[-1].rstrip("\n").rstrip("\r")

    out_lines.extend(src_lines[src_idx:])
    return "".join(out_lines)


def _apply_file_patch_with_fallback(
    existing: str | None,
    file_patch: FilePatch,
    display_path: str,
    *,
    newline_style: str,
    exact_error: PatchApplyError,
) -> str | None:
    if file_patch.new_path == "/dev/null":
        raise exact_error
    current = existing if existing is not None else ""
    src_lines = current.splitlines(keepends=True)
    out_lines: list[str] = []
    src_idx = 0
    max_displacement = 6
    for h_idx, hunk in enumerate(file_patch.hunks, start=1):
        expected_idx = max(hunk.old_start - 1, 0)
        pattern = [line[1:] for line in hunk.lines if line and line[0] in {" ", "-"}]
        candidate_indexes = _find_fuzzy_candidates(src_lines, pattern, expected_idx, max_displacement)
        if len(candidate_indexes) != 1:
            raise PatchApplyError(
                f"{display_path}: exact patch failed ({exact_error}); fuzzy fallback "
                f"{'ambiguous' if candidate_indexes else 'found no candidate'} for hunk #{h_idx}"
            ) from exact_error
        target_idx = candidate_indexes[0]
        if target_idx < src_idx:
            raise PatchApplyError(f"{display_path}: overlapping hunk #{h_idx} under fuzzy fallback") from exact_error
        out_lines.extend(src_lines[src_idx:target_idx])
        src_idx = target_idx
        for lno, hline in enumerate(hunk.lines, start=1):
            if not hline:
                continue
            tag = hline[0]
            payload = hline[1:]
            if tag == "\\":
                continue
            if tag in {" ", "-"}:
                _expect_line_fuzzy(src_lines, src_idx, payload, display_path, h_idx, lno, tag)
                if tag == " ":
                    out_lines.append(src_lines[src_idx])
                src_idx += 1
            elif tag == "+":
                out_lines.append(payload + newline_style)
            else:
                raise PatchApplyError(f"{display_path}: unsupported hunk line tag {tag!r} in hunk #{h_idx}") from exact_error
            if tag == "+" and _next_line_has_no_newline_marker(hunk.lines, lno - 1) and out_lines:
                out_lines[-1] = out_lines[-1].rstrip("\n").rstrip("\r")
    out_lines.extend(src_lines[src_idx:])
    return "".join(out_lines)


def _expect_line(src_lines: list[str], index: int, expected: str, display_path: str, hunk_index: int, line_index: int, kind: str) -> None:
    if index >= len(src_lines):
        raise PatchApplyError(f"{display_path}: hunk #{hunk_index} {kind} line {line_index} out of bounds")
    actual = src_lines[index].rstrip("\n").rstrip("\r")
    if actual != expected:
        raise PatchApplyError(
            f"{display_path}: hunk #{hunk_index} {kind} mismatch at source line {index + 1}; expected {expected!r}, got {actual!r}"
        )


def _expect_line_fuzzy(
    src_lines: list[str], index: int, expected: str, display_path: str, hunk_index: int, line_index: int, tag: str
) -> None:
    if index >= len(src_lines):
        raise PatchApplyError(f"{display_path}: fuzzy hunk #{hunk_index} line {line_index} out of bounds")
    actual = src_lines[index].rstrip("\n").rstrip("\r")
    if _normalize_whitespace(actual) != _normalize_whitespace(expected):
        kind = "context" if tag == " " else "remove"
        raise PatchApplyError(
            f"{display_path}: fuzzy hunk #{hunk_index} {kind} mismatch at source line {index + 1}; "
            f"expected {expected!r}, got {actual!r}"
        )


def _find_fuzzy_candidates(
    src_lines: list[str], pattern: list[str], expected_idx: int, max_displacement: int
) -> list[int]:
    if not pattern:
        return [expected_idx]
    min_idx = max(0, expected_idx - max_displacement)
    max_idx = min(len(src_lines) - len(pattern), expected_idx + max_displacement)
    candidates: list[int] = []
    for start in range(min_idx, max_idx + 1):
        matched = True
        for offset, expected in enumerate(pattern):
            actual = src_lines[start + offset].rstrip("\n").rstrip("\r")
            if _normalize_whitespace(actual) != _normalize_whitespace(expected):
                matched = False
                break
        if matched:
            candidates.append(start)
    return candidates


def _normalize_whitespace(value: str) -> str:
    return "".join(value.split())


def _detect_newline_style(content: str | None) -> str:
    if content and "\r\n" in content:
        return "\r\n"
    if content and "\r" in content:
        return "\r"
    return "\n"


def _next_line_has_no_newline_marker(lines: list[str], index: int) -> bool:
    next_index = index + 1
    if next_index >= len(lines):
        return False
    return lines[next_index].startswith("\\ No newline at end of file")
