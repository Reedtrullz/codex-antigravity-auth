#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:51122/v1"
DEFAULT_TOKEN_ENV = "ANTIGRAVITY_GATEWAY_TOKEN"
MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "claude-opus": "claude-opus-4-6",
    "claude-opus-4-6": "claude-opus-4-6",
    "sonnet": "claude-3.5-sonnet",
    "claude-sonnet": "claude-3.5-sonnet",
    "claude-3.5-sonnet": "claude-3.5-sonnet",
    "claude-3-5-sonnet": "claude-3.5-sonnet",
}
DEFAULT_REVIEW_MODEL = "claude-opus-4-6"
DEFAULT_CONSULT_MODEL = "claude-3.5-sonnet"
DEFAULT_PLAN_MODEL = "claude-opus-4-6"
MAX_FILE_BYTES = 180_000
DEFAULT_MAX_PROMPT_CHARS = 120_000
DEFAULT_MAX_SYNTHESIS_CHARS = DEFAULT_MAX_PROMPT_CHARS
PID_FILE = Path.home() / ".codex" / "anti-gateway.pid"
LOG_FILE = Path.home() / ".codex" / "anti-gateway.log"

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".aws",
    ".azure",
    ".venv",
    ".config",
    ".gcloud",
    ".gnupg",
    ".ssh",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
    "dist",
    "build",
    ".build",
    ".deriveddata",
    "target",
    "credential",
    "credentials",
    "keychain",
    "keys",
    "private",
    "secret",
    "secrets",
    "tokens",
}
EXCLUDED_NAMES = {
    ".env",
    ".env.local",
    ".envrc",
    "antigravity-accounts.json",
    "antigravity-providers.json",
    "antigravity-credentials.json",
    "antigravity-storage.key",
    "id_rsa",
    "id_ed25519",
    "known_hosts",
}
EXCLUDED_PATTERNS = [
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*credential*.json",
    "*credentials*.json",
    "*secret*.env",
    "*secret*.json",
    "*secret*.toml",
    "*secret*.txt",
    "*secret*.yaml",
    "*secret*.yml",
    "*token*.env",
    "*token*.json",
    "*token*.toml",
    "*token*.txt",
    "*token*.yaml",
    "*token*.yml",
    "*apikey*",
    "*api-key*",
]


class AntiError(Exception):
    pass


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def normalize_base_url(value: str) -> str:
    value = str(value).strip()
    if not value:
        raise AntiError("base URL must be non-empty")
    return value.rstrip("/")


def resolve_model(value: str | None, *, default: str) -> str:
    raw = (value or default).strip()
    return MODEL_ALIASES.get(raw.lower(), raw)


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def token_from_env(env_name: str) -> str | None:
    token = os.environ.get(env_name, "")
    return token if token else None


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
    token_env: str = DEFAULT_TOKEN_ENV,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    token = token_from_env(token_env)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            status = int(res.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
    except Exception as exc:
        raise AntiError(f"request to {url} failed: {exc}") from exc

    if not raw:
        return status, {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise AntiError(f"request to {url} returned non-JSON response") from exc
    if not isinstance(decoded, dict):
        raise AntiError(f"request to {url} returned JSON {type(decoded).__name__}, expected object")
    return status, decoded


def model_ids_from_catalog(payload: dict[str, Any]) -> set[str]:
    entries = payload.get("data")
    if not isinstance(entries, list):
        entries = payload.get("models")
    if not isinstance(entries, list):
        return set()
    ids: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            ids.add(entry["id"])
    return ids


def fetch_model_ids(base_url: str, *, timeout: float, token_env: str) -> set[str]:
    status, payload = request_json(
        "GET",
        f"{normalize_base_url(base_url)}/models",
        timeout=timeout,
        token_env=token_env,
    )
    if status != 200:
        detail = payload.get("detail") or payload.get("error") or payload
        raise AntiError(f"/v1/models returned HTTP {status}: {detail}")
    ids = model_ids_from_catalog(payload)
    if not ids:
        raise AntiError("/v1/models returned no usable model ids")
    return ids


def validate_git_rev_range(value: str, *, source: str) -> str:
    value = value.strip()
    if not value:
        raise AntiError(f"{source} must be non-empty")
    if value.startswith("-"):
        raise AntiError(f"{source} must not start with '-'")
    if "\0" in value or "\n" in value or "\r" in value:
        raise AntiError(f"{source} must be a single git revision/range argument")
    return value


def extract_response_text(payload: Any) -> str:
    texts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("output_text"), str):
                texts.append(value["output_text"])
            if isinstance(value.get("text"), str) and value.get("type") in {
                "output_text",
                "text",
                "message",
            }:
                texts.append(value["text"])
            for key in ("output", "content", "response"):
                if key in value:
                    visit(value[key])
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    merged = "\n".join(part.strip() for part in texts if part and part.strip()).strip()
    if merged:
        return merged
    return json.dumps(payload, indent=2, sort_keys=True)[:8000]


def post_response(
    *,
    base_url: str,
    model: str,
    prompt: str,
    max_output_tokens: int,
    timeout: float,
    token_env: str,
    retries: int = 0,
) -> str:
    model_ids = fetch_model_ids(base_url, timeout=timeout, token_env=token_env)
    if model not in model_ids:
        sample = ", ".join(sorted(model_ids)[:12])
        raise AntiError(f"model {model!r} is not advertised by /v1/models. Available sample: {sample}")
    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": max_output_tokens,
        "stream": False,
    }
    attempts = max(0, retries) + 1
    retryable_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
    last_error: str | None = None
    response_url = f"{normalize_base_url(base_url)}/responses"
    for attempt in range(1, attempts + 1):
        try:
            status, decoded = request_json(
                "POST",
                response_url,
                payload=payload,
                timeout=timeout,
                token_env=token_env,
            )
        except AntiError as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(min(4.0, 0.75 * attempt))
                continue
            raise AntiError(
                "request failed after "
                f"{attempt} attempt(s): {last_error}. Diagnostics: "
                f"model={model}, prompt_chars={len(prompt)}, timeout={timeout}, gateway={base_url}"
            ) from exc

        if status == 200:
            return extract_response_text(decoded)

        detail = decoded.get("detail") or decoded.get("error") or decoded
        last_error = f"HTTP {status}: {detail}"
        if status in retryable_statuses and attempt < attempts:
            time.sleep(min(4.0, 0.75 * attempt))
            continue
        raise AntiError(
            f"/v1/responses returned {last_error} after {attempt} attempt(s). Diagnostics: "
            f"model={model}, prompt_chars={len(prompt)}, timeout={timeout}, gateway={base_url}, "
            f"retryable={str(status in retryable_statuses).lower()}"
        )

    raise AssertionError("post_response retry loop should have returned or raised")


def find_repo_root(start: Path) -> Path | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def run_git(root: Path, args: list[str], *, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise AntiError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def path_is_excluded(rel_path: str) -> bool:
    path = rel_path.replace("\\", "/")
    path_lower = path.lower()
    parts = [part.lower() for part in path.split("/") if part]
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    name = parts[-1] if parts else ""
    if name in EXCLUDED_NAMES:
        return True
    patterns = [pattern.lower() for pattern in EXCLUDED_PATTERNS]
    if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
        return True
    if any(fnmatch.fnmatch(part, pattern) for part in parts for pattern in patterns):
        return True
    return any(fnmatch.fnmatch(path_lower, pattern) for pattern in patterns)


def relative_safe_path(root: Path, raw_path: str) -> str:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        rel = resolved.relative_to(root.resolve())
    except Exception as exc:
        raise AntiError(f"refusing path outside review root: {raw_path}") from exc
    return rel.as_posix()


def filter_paths(paths: list[str], *, root: Path) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    excluded: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        rel = relative_safe_path(root, raw)
        if rel in seen:
            continue
        seen.add(rel)
        if path_is_excluded(rel):
            excluded.append(rel)
        else:
            kept.append(rel)
    return kept, excluded


def read_paths_file(spec: str) -> list[str]:
    if spec == "-":
        raw = sys.stdin.buffer.read()
    else:
        raw = Path(spec).expanduser().read_bytes()
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AntiError(f"path list {spec!r} is not valid UTF-8") from exc
    if "\0" in decoded:
        return [item for item in decoded.split("\0") if item]
    return [line for line in decoded.splitlines() if line]


def selected_paths_from_args(args: argparse.Namespace) -> list[str]:
    paths = list(getattr(args, "file", None) or [])
    for spec in getattr(args, "files_from", None) or []:
        paths.extend(read_paths_file(spec))
    return paths


def review_rev_range(args: argparse.Namespace) -> str | None:
    explicit = getattr(args, "changed_files_range", None)
    if explicit:
        return validate_git_rev_range(explicit, source="--changed-files")
    base = getattr(args, "base", None)
    if base:
        return f"{validate_git_rev_range(base, source='--base')}...HEAD"
    return None


def changed_paths(
    root: Path,
    scope: str,
    selected: list[str],
    *,
    rev_range: str | None = None,
) -> tuple[list[str], list[str]]:
    if selected:
        return filter_paths(selected, root=root)
    if scope == "staged":
        raw = run_git(root, ["diff", "--cached", "--name-only", "--diff-filter=ACMRT"])
    elif scope == "working-tree":
        raw = run_git(root, ["diff", "HEAD", "--name-only", "--diff-filter=ACMRT"])
    elif scope == "diff":
        if not rev_range:
            raise AntiError("--scope diff requires --base or --changed-files")
        rev_range = validate_git_rev_range(rev_range, source="revision range")
        raw = run_git(root, ["diff", "--name-only", "--diff-filter=ACMRT", rev_range])
    elif scope == "files":
        raise AntiError("--scope files requires at least one --file")
    else:
        raise AntiError(f"unsupported review scope: {scope}")
    return filter_paths(raw.splitlines(), root=root)


def diff_for_paths(root: Path, scope: str, paths: list[str], *, rev_range: str | None = None) -> str:
    if not paths or scope == "files":
        return ""
    if scope == "staged":
        return run_git(root, ["diff", "--cached", "--no-ext-diff", "--", *paths], check=False)
    if scope == "diff":
        if not rev_range:
            raise AntiError("--scope diff requires --base or --changed-files")
        rev_range = validate_git_rev_range(rev_range, source="revision range")
        return run_git(root, ["diff", "--no-ext-diff", rev_range, "--", *paths], check=False)
    return run_git(root, ["diff", "HEAD", "--no-ext-diff", "--", *paths], check=False)


def file_is_tracked(root: Path, rel_path: str) -> bool:
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", rel_path],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode == 0


def read_text_file(root: Path, rel_path: str) -> tuple[str, str | None]:
    path = root / rel_path
    if not path.is_file():
        return "", f"{rel_path}: not a regular file"
    raw = path.read_bytes()
    if b"\0" in raw:
        return "", f"{rel_path}: binary file skipped"
    note = None
    if len(raw) > MAX_FILE_BYTES:
        original_len = len(raw)
        raw = raw[:MAX_FILE_BYTES]
        note = f"{rel_path}: truncated to {MAX_FILE_BYTES} bytes ({original_len} original bytes)"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if note and exc.start >= max(0, len(raw) - 4):
            raw = raw[: exc.start]
            text = raw.decode("utf-8")
            note += "; trimmed partial UTF-8 character at truncation boundary"
        else:
            return "", f"{rel_path}: non-UTF-8 file skipped"
    return text, note


def apply_prompt_limit(prompt: str, max_prompt_chars: int, caveats: list[str]) -> str:
    if max_prompt_chars > 0 and len(prompt) > max_prompt_chars:
        caveats.append(f"Prompt truncated to {max_prompt_chars} characters")
        return prompt[:max_prompt_chars]
    return prompt


def truncate_at_line_boundary(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    newline = truncated.rfind("\n")
    if newline > max_chars // 2:
        return truncated[:newline]
    return truncated


def review_prompt_parts(
    *,
    scope_line: str,
    diff: str,
    included_files: list[tuple[str, str]],
    omitted_files: list[str],
    excluded: list[str],
    caveats: list[str],
) -> list[str]:
    incomplete = bool(omitted_files) or any("truncated" in caveat.lower() for caveat in caveats)
    manifest_lines = [
        "## Review Manifest",
        f"- status: {'incomplete' if incomplete else 'complete'}",
        f"- scope: {scope_line}",
        f"- included_files: {', '.join(path for path, _text in included_files) if included_files else 'none'}",
        f"- omitted_files: {', '.join(omitted_files) if omitted_files else 'none'}",
        f"- excluded_paths: {', '.join(excluded[:20]) if excluded else 'none'}",
    ]
    if caveats:
        manifest_lines.append("- helper_warnings:")
        manifest_lines.extend(f"  - {caveat}" for caveat in caveats)
    else:
        manifest_lines.append("- helper_warnings: none")

    parts = [
        "You are an Antigravity sidecar reviewer for a Codex coding session.",
        "Review independently. Lead with concrete defects, regressions, security risks, install/usability problems, or missing tests. Avoid speculative style comments.",
        "Use file paths and precise behavior references when possible. If you find no issues, say so and list residual verification caveats.",
        "Treat the Review Manifest as authoritative. Helper warnings, omitted files, and partial diffs are scope caveats, not source-code defects.",
        "\n".join(manifest_lines),
    ]
    if diff.strip():
        parts.append("## Git Diff\n```diff\n" + diff + "\n```")
    if included_files:
        blocks = [f"### {rel}\n```text\n{text}\n```" for rel, text in included_files]
        parts.append("## File Contents\n" + "\n\n".join(blocks))
    if not diff.strip() and not included_files:
        parts.append("No diff or file content was available in the requested scope. Explain that limitation.")
    return parts


def build_review_prompt(
    *,
    scope_line: str,
    diff: str,
    file_texts: list[tuple[str, str]],
    excluded: list[str],
    initial_caveats: list[str],
    max_prompt_chars: int,
) -> tuple[str, list[str], dict[str, Any]]:
    caveats = list(initial_caveats)
    diff_for_prompt = diff
    omitted_files = [rel for rel, text in file_texts if not text]
    candidates = [(rel, text) for rel, text in file_texts if text]
    included: list[tuple[str, str]] = []

    if max_prompt_chars > 0 and diff_for_prompt:
        prompt_without_files = "\n\n".join(
            review_prompt_parts(
                scope_line=scope_line,
                diff=diff_for_prompt,
                included_files=[],
                omitted_files=[rel for rel, _text in candidates],
                excluded=excluded,
                caveats=caveats,
            )
        )
        if len(prompt_without_files) > max_prompt_chars:
            base_parts = review_prompt_parts(
                scope_line=scope_line,
                diff="",
                included_files=[],
                omitted_files=[rel for rel, _text in candidates],
                excluded=excluded,
                caveats=caveats,
            )
            base_len = len("\n\n".join(base_parts))
            available = max(0, max_prompt_chars - base_len - len("\n\n## Git Diff\n```diff\n\n```"))
            diff_for_prompt = truncate_at_line_boundary(diff_for_prompt, available)
            caveats.append(
                f"Git diff truncated to fit max prompt budget ({len(diff)} original chars, {len(diff_for_prompt)} included)"
            )

    for index, (rel, text) in enumerate(candidates):
        trial_included = [*included, (rel, text)]
        trial_omitted = [item_rel for item_rel, _item_text in candidates[index + 1 :]]
        trial_omitted.extend(omitted_files)
        trial_prompt = "\n\n".join(
            review_prompt_parts(
                scope_line=scope_line,
                diff=diff_for_prompt,
                included_files=trial_included,
                omitted_files=trial_omitted,
                excluded=excluded,
                caveats=caveats,
            )
        )
        if max_prompt_chars <= 0 or len(trial_prompt) <= max_prompt_chars:
            included = trial_included
        else:
            omitted_files.append(f"{rel} (omitted to keep whole-file prompt under {max_prompt_chars} chars)")

    prompt = "\n\n".join(
        review_prompt_parts(
            scope_line=scope_line,
            diff=diff_for_prompt,
            included_files=included,
            omitted_files=omitted_files,
            excluded=excluded,
            caveats=caveats,
        )
    )
    metadata = {
        "status": "incomplete" if omitted_files or any("truncated" in item.lower() for item in caveats) else "complete",
        "prompt_chars": len(prompt),
        "diff_chars": len(diff_for_prompt),
        "diff_truncated": diff_for_prompt != diff,
        "included_files": [rel for rel, _text in included],
        "omitted_files": omitted_files,
        "excluded_paths": excluded,
        "helper_warnings": caveats,
    }
    return prompt, caveats, metadata


def collect_review_context(args: argparse.Namespace) -> dict[str, Any]:
    root = find_repo_root(Path.cwd())
    if root is None:
        if args.scope != "files":
            raise AntiError("review requires a git repository unless --scope files is used")
        root = Path.cwd().resolve()

    selected = selected_paths_from_args(args)
    rev_range = review_rev_range(args)
    paths, excluded = changed_paths(root, args.scope, selected, rev_range=rev_range)
    diff = diff_for_paths(root, args.scope, paths, rev_range=rev_range)
    notes: list[str] = []
    file_texts: list[tuple[str, str]] = []

    include_file_text = args.scope == "files"
    for rel in paths:
        if include_file_text or not file_is_tracked(root, rel):
            text, note = read_text_file(root, rel)
            if note:
                notes.append(note)
            if text:
                file_texts.append((rel, text))
            else:
                file_texts.append((rel, ""))

    scope_line = args.scope
    if rev_range:
        scope_line += f" ({rev_range})"
    if paths:
        scope_line += " over " + ", ".join(paths[:20])
        if len(paths) > 20:
            scope_line += f", ... ({len(paths)} files total)"

    caveats: list[str] = []
    if excluded:
        caveats.append("Excluded sensitive/cache/binary-looking paths: " + ", ".join(excluded[:20]))
    if notes:
        caveats.extend(notes)
    return {
        "root": root,
        "paths": paths,
        "excluded": excluded,
        "diff": diff,
        "file_texts": file_texts,
        "scope_line": scope_line,
        "caveats": caveats,
    }


def assemble_review_prompt_from_context(
    context: dict[str, Any],
    *,
    max_prompt_chars: int,
) -> tuple[str, list[str], list[str], dict[str, Any]]:
    prompt, caveats, metadata = build_review_prompt(
        scope_line=context["scope_line"],
        diff=context["diff"],
        file_texts=context["file_texts"],
        excluded=context["excluded"],
        initial_caveats=context["caveats"],
        max_prompt_chars=max_prompt_chars,
    )
    return prompt, context["paths"], caveats, metadata


def assemble_review_prompt(args: argparse.Namespace) -> tuple[str, list[str], list[str], dict[str, Any]]:
    context = collect_review_context(args)
    return assemble_review_prompt_from_context(context, max_prompt_chars=args.max_prompt_chars)


def split_text_by_budget(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_chars:
            chunks.append(rest)
            break
        piece = truncate_at_line_boundary(rest, max_chars)
        if not piece:
            piece = rest[:max_chars]
        chunks.append(piece)
        rest = rest[len(piece) :].lstrip("\n")
    return chunks


def prompt_fits(prompt: str, max_prompt_chars: int) -> bool:
    return max_prompt_chars <= 0 or len(prompt) <= max_prompt_chars


def build_review_chunk_prompts(
    context: dict[str, Any],
    *,
    max_prompt_chars: int,
    max_chunks: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    chunk_budget = max(1200, max_prompt_chars - 1800) if max_prompt_chars > 0 else 0
    chunks: list[dict[str, Any]] = []
    omitted_items: list[str] = []

    def add_chunk(kind: str, label: str, prompt: str, metadata: dict[str, Any]) -> None:
        if len(chunks) >= max_chunks:
            omitted_items.append(label)
            return
        chunks.append(
            {
                "kind": kind,
                "label": label,
                "prompt": prompt,
                "metadata": metadata,
                "prompt_chars": len(prompt),
            }
        )

    diff = str(context["diff"])
    if diff.strip():
        diff_parts = split_text_by_budget(diff, chunk_budget)
        for index, diff_part in enumerate(diff_parts, start=1):
            label = f"diff part {index}/{len(diff_parts)}"
            scope_line = f"{context['scope_line']} ({label})"
            prompt, caveats, metadata = build_review_prompt(
                scope_line=scope_line,
                diff=diff_part,
                file_texts=[],
                excluded=context["excluded"],
                initial_caveats=[
                    *context["caveats"],
                    f"Chunked review: {label}; synthesize with other chunks before final judgment.",
                ],
                max_prompt_chars=max_prompt_chars,
            )
            metadata["chunk_kind"] = "diff"
            metadata["chunk_label"] = label
            if not prompt_fits(prompt, max_prompt_chars):
                omitted_items.append(f"{label} (prompt still exceeds {max_prompt_chars} chars)")
                continue
            add_chunk("diff", label, prompt, metadata)

    file_items: list[tuple[str, str]] = []
    for rel, text in context["file_texts"]:
        if not text:
            omitted_items.append(rel)
            continue
        whole_prompt, _whole_caveats, whole_metadata = build_review_prompt(
            scope_line=f"{context['scope_line']} ({rel})",
            diff="",
            file_texts=[(rel, text)],
            excluded=context["excluded"],
            initial_caveats=context["caveats"],
            max_prompt_chars=max_prompt_chars,
        )
        if prompt_fits(whole_prompt, max_prompt_chars) and whole_metadata.get("included_files") == [rel]:
            file_items.append((rel, text))
            continue
        text_parts = split_text_by_budget(text, chunk_budget)
        for index, text_part in enumerate(text_parts, start=1):
            label = f"{rel} part {index}/{len(text_parts)}"
            file_items.append((label, text_part))

    current: list[tuple[str, str]] = []
    for rel, text in file_items:
        trial = [*current, (rel, text)]
        prompt, caveats, metadata = build_review_prompt(
            scope_line=f"{context['scope_line']} (file chunk)",
            diff="",
            file_texts=trial,
            excluded=context["excluded"],
            initial_caveats=[
                *context["caveats"],
                "Chunked review: file chunk; synthesize with other chunks before final judgment.",
            ],
            max_prompt_chars=max_prompt_chars,
        )
        if prompt_fits(prompt, max_prompt_chars) and not metadata["omitted_files"]:
            current = trial
            continue
        if current:
            current_prompt, _current_caveats, current_metadata = build_review_prompt(
                scope_line=f"{context['scope_line']} (file chunk)",
                diff="",
                file_texts=current,
                excluded=context["excluded"],
                initial_caveats=[
                    *context["caveats"],
                    "Chunked review: file chunk; synthesize with other chunks before final judgment.",
                ],
                max_prompt_chars=max_prompt_chars,
            )
            label = ", ".join(path for path, _item_text in current)
            current_metadata["chunk_kind"] = "files"
            current_metadata["chunk_label"] = label
            add_chunk("files", label, current_prompt, current_metadata)
        current = [(rel, text)]

    if current:
        current_prompt, _current_caveats, current_metadata = build_review_prompt(
            scope_line=f"{context['scope_line']} (file chunk)",
            diff="",
            file_texts=current,
            excluded=context["excluded"],
            initial_caveats=[
                *context["caveats"],
                "Chunked review: file chunk; synthesize with other chunks before final judgment.",
            ],
            max_prompt_chars=max_prompt_chars,
        )
        label = ", ".join(path for path, _item_text in current)
        current_metadata["chunk_kind"] = "files"
        current_metadata["chunk_label"] = label
        if prompt_fits(current_prompt, max_prompt_chars):
            add_chunk("files", label, current_prompt, current_metadata)
        else:
            omitted_items.append(f"{label} (prompt still exceeds {max_prompt_chars} chars)")

    metadata = {
        "chunk_count": len(chunks),
        "max_chunks": max_chunks,
        "omitted_items": omitted_items,
        "status": "incomplete" if omitted_items else "complete",
    }
    return chunks, metadata


def build_chunk_synthesis_prompt(
    *,
    context: dict[str, Any],
    chunks: list[dict[str, Any]],
    chunk_outputs: list[str],
    chunk_metadata: dict[str, Any],
    max_chars: int,
) -> tuple[str, list[str], dict[str, Any]]:
    manifest = {
        "scope": context["scope_line"],
        "chunk_count": len(chunks),
        "omitted_items": chunk_metadata.get("omitted_items", []),
        "chunk_labels": [chunk["label"] for chunk in chunks],
        "status": chunk_metadata.get("status", "complete"),
    }

    def render(outputs: list[str]) -> str:
        chunk_sections = []
        for index, (chunk, output) in enumerate(zip(chunks, outputs), start=1):
            chunk_sections.append(
                "\n".join(
                    [
                        f"## Chunk {index}: {chunk['label']}",
                        f"- kind: {chunk['kind']}",
                        f"- prompt_chars: {chunk['prompt_chars']}",
                        output.strip(),
                    ]
                )
            )
        return "\n\n".join(
            [
                "You are synthesizing an Antigravity sidecar code review that was split into multiple bounded chunks.",
                "Use only the chunk findings below. Separate confirmed defects from risks and scope caveats. Do not invent findings for omitted items.",
                "If chunks disagree or a finding depends on omitted context, mark it as needing local verification.",
                "## Chunked Review Manifest\n```json\n" + json.dumps(manifest, indent=2, sort_keys=True) + "\n```",
                *chunk_sections,
            ]
        )

    outputs = [output.strip() for output in chunk_outputs]
    prompt = render(outputs)
    original_len = len(prompt)
    caveats: list[str] = []
    metadata: dict[str, Any] = {
        "synthesis_prompt_original_chars": original_len,
        "synthesis_truncated_outputs": [],
    }
    if max_chars <= 0 or len(prompt) <= max_chars or not outputs:
        metadata["synthesis_prompt_chars"] = len(prompt)
        return prompt, caveats, metadata

    marker = "\n[Chunk output truncated by helper to keep synthesis prompt bounded.]"
    empty_prompt_len = len(render([""] * len(outputs)))
    available_for_outputs = max_chars - empty_prompt_len - (len(marker) * len(outputs))
    truncated_labels: list[str] = []

    if available_for_outputs <= 0:
        limited_outputs = [marker.strip() for _output in outputs]
        truncated_labels = [chunk["label"] for chunk in chunks]
    else:
        per_output_budget = max(1, available_for_outputs // len(outputs))
        limited_outputs = []
        for chunk, output in zip(chunks, outputs):
            if len(output) <= per_output_budget:
                limited_outputs.append(output)
                continue
            cut = truncate_at_line_boundary(output, per_output_budget)
            if len(cut) > per_output_budget:
                cut = cut[:per_output_budget]
            limited_outputs.append((cut + marker).strip() if cut else marker.strip())
            truncated_labels.append(chunk["label"])

    prompt = render(limited_outputs)
    if len(prompt) > max_chars:
        prompt = truncate_at_line_boundary(prompt, max_chars)
        if len(prompt) > max_chars:
            prompt = prompt[:max_chars]
        if not truncated_labels:
            truncated_labels = [chunk["label"] for chunk in chunks]

    caveats.append(
        f"Synthesis chunk outputs truncated to keep prompt under {max_chars} characters "
        f"({original_len} original chars)"
    )
    metadata["synthesis_prompt_chars"] = len(prompt)
    metadata["synthesis_truncated_outputs"] = truncated_labels
    return prompt, caveats, metadata


def should_run_chunked_review(args: argparse.Namespace, metadata: dict[str, Any]) -> bool:
    mode = getattr(args, "chunked", "auto")
    if mode == "off":
        return False
    if mode == "always":
        return True
    return metadata.get("status") == "incomplete" or bool(metadata.get("omitted_files")) or bool(
        metadata.get("diff_truncated")
    )


def run_chunked_review(
    *,
    args: argparse.Namespace,
    context: dict[str, Any],
    model: str,
    base_metadata: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any]]:
    chunks, chunk_metadata = build_review_chunk_prompts(
        context,
        max_prompt_chars=args.max_prompt_chars,
        max_chunks=args.max_review_chunks,
    )
    if not chunks:
        raise AntiError("chunked review produced no reviewable chunks; narrow the file set or raise --max-prompt-chars")

    chunk_outputs: list[str] = []
    for chunk in chunks:
        chunk_outputs.append(
            post_response(
                base_url=args.base_url,
                model=model,
                prompt=chunk["prompt"],
                max_output_tokens=args.chunk_output_tokens,
                timeout=args.timeout,
                token_env=args.gateway_token_env,
                retries=args.retry,
            )
        )

    synthesis_prompt, synthesis_caveats, synthesis_metadata = build_chunk_synthesis_prompt(
        context=context,
        chunks=chunks,
        chunk_outputs=chunk_outputs,
        chunk_metadata=chunk_metadata,
        max_chars=args.max_synthesis_chars,
    )
    caveats = list(context["caveats"])
    caveats.extend(synthesis_caveats)
    synthesis = post_response(
        base_url=args.base_url,
        model=model,
        prompt=synthesis_prompt,
        max_output_tokens=args.max_output_tokens,
        timeout=args.timeout,
        token_env=args.gateway_token_env,
        retries=args.retry,
    )
    if chunk_metadata["omitted_items"]:
        caveats.append("Chunked review omitted items: " + ", ".join(chunk_metadata["omitted_items"][:20]))
    metadata = {
        **base_metadata,
        "status": "incomplete" if chunk_metadata["omitted_items"] else "complete",
        "chunked": True,
        "single_prompt_status": base_metadata.get("status"),
        "single_prompt_omitted_files": base_metadata.get("omitted_files", []),
        "omitted_files": chunk_metadata["omitted_items"],
        "chunk_count": len(chunks),
        "chunk_prompts": [
            {
                "index": index,
                "kind": chunk["kind"],
                "label": chunk["label"],
                "prompt_chars": chunk["prompt_chars"],
            }
            for index, chunk in enumerate(chunks, start=1)
        ],
        "chunk_omitted_items": chunk_metadata["omitted_items"],
        **synthesis_metadata,
    }
    return synthesis, caveats, metadata


def assemble_plan_prompt(args: argparse.Namespace) -> tuple[str, list[str]]:
    user_goal = read_prompt(args)
    context = ""
    caveats: list[str] = []

    if args.scope != "none":
        root = find_repo_root(Path.cwd())
        if root is None:
            if args.scope != "files":
                raise AntiError("plan context requires a git repository unless --scope files is used")
            root = Path.cwd().resolve()

        paths, excluded = changed_paths(root, args.scope, args.file or [])
        diff = diff_for_paths(root, args.scope, paths)
        notes: list[str] = []
        file_blocks: list[str] = []
        include_file_text = args.scope == "files"

        for rel in paths:
            if include_file_text or not file_is_tracked(root, rel):
                text, note = read_text_file(root, rel)
                if note:
                    notes.append(note)
                if text:
                    file_blocks.append(f"### {rel}\n```text\n{text}\n```")

        scope_line = args.scope
        if paths:
            scope_line += " over " + ", ".join(paths[:20])
            if len(paths) > 20:
                scope_line += f", ... ({len(paths)} files total)"

        context_parts = [f"Planning context scope: {scope_line}."]
        if diff.strip():
            context_parts.append("## Git Diff\n```diff\n" + diff + "\n```")
        if file_blocks:
            context_parts.append("## File Contents\n" + "\n\n".join(file_blocks))
        if not diff.strip() and not file_blocks:
            context_parts.append("No diff or file content was available in the requested scope.")
        context = "\n\n".join(context_parts)

        if excluded:
            caveats.append("Excluded sensitive/cache/binary-looking paths: " + ", ".join(excluded[:20]))
        caveats.extend(notes)

    prompt = "\n".join(
        part
        for part in [
            "You are Claude Opus acting as an Antigravity deep-work planning lane for a Codex coding session.",
            "Produce a decision-complete plan for a long autonomous engineering session. Optimize for correctness, sequencing, verification, and keeping the main Codex agent unblocked.",
            "The plan must be executable by another senior agent without needing to make major decisions. Include: goal framing, phase order, task decomposition, critical path, parallelizable work, risks, checkpoints, validation commands, rollback/stop conditions, and explicit non-claims.",
            "Prefer concrete actions over generic advice. If repository context is incomplete, say exactly what is missing and how to gather it before execution.",
            f"User goal:\n{user_goal}",
            context,
        ]
        if part
    )

    prompt = apply_prompt_limit(prompt, args.max_prompt_chars, caveats)
    return prompt, caveats


def read_prompt(args: argparse.Namespace) -> str:
    pieces: list[str] = []
    if args.prompt_file:
        path = Path(args.prompt_file).expanduser()
        raw = path.read_bytes()
        if b"\0" in raw:
            raise AntiError("prompt file looks binary")
        pieces.append(raw.decode("utf-8"))
    if args.prompt:
        pieces.append(args.prompt)
    if getattr(args, "prompt_parts", None):
        pieces.append(" ".join(args.prompt_parts))
    prompt = "\n\n".join(part.strip() for part in pieces if part and part.strip()).strip()
    if not prompt:
        raise AntiError("provide --prompt, --prompt-file, or a positional prompt")
    return prompt


def print_result(
    *,
    mode: str,
    model: str,
    base_url: str,
    text: str,
    caveats: list[str] | None = None,
    output_json: bool = False,
    metadata: dict[str, Any] | None = None,
) -> None:
    if output_json:
        print(
            json.dumps(
                {
                    "mode": mode,
                    "model": model,
                    "gateway": base_url,
                    "caveats": caveats or [],
                    "metadata": metadata or {},
                    "output_text": text.strip(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    print(f"## Antigravity {mode} ({model})")
    print(f"- Gateway: {base_url}")
    if metadata and metadata.get("status"):
        print(f"- Status: {metadata['status']}")
    if caveats:
        for caveat in caveats:
            print(f"- Caveat: {caveat}")
    print()
    print(text.strip())


def find_cli() -> tuple[list[str], Path | None]:
    found = shutil.which("codex-antigravity")
    if found:
        return [found], None
    start = Path.cwd().resolve()
    for candidate in [start, *start.parents]:
        if (candidate / "codex_antigravity_auth" / "cli.py").exists():
            return [sys.executable, "-m", "codex_antigravity_auth.cli"], candidate
    raise AntiError("codex-antigravity CLI was not found on PATH and no source checkout was found above cwd")


def run_cli(args: list[str]) -> int:
    cmd, cwd = find_cli()
    proc = subprocess.run([*cmd, *args], cwd=cwd)
    return int(proc.returncode)


def run_cli_quiet(args: list[str]) -> int:
    cmd, cwd = find_cli()
    proc = subprocess.run(
        [*cmd, *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return int(proc.returncode)


def check_gateway(base_url: str, *, timeout: float, token_env: str) -> bool:
    try:
        fetch_model_ids(base_url, timeout=timeout, token_env=token_env)
        return True
    except AntiError:
        return False


def command_consult(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_CONSULT_MODEL)
    prompt = read_prompt(args)
    caveats: list[str] = []
    prompt = apply_prompt_limit(prompt, args.max_prompt_chars, caveats)
    text = post_response(
        base_url=args.base_url,
        model=model,
        prompt=prompt,
        max_output_tokens=args.max_output_tokens,
        timeout=args.timeout,
        token_env=args.gateway_token_env,
        retries=args.retry,
    )
    print_result(
        mode="consult",
        model=model,
        base_url=args.base_url,
        text=text,
        caveats=caveats,
        output_json=args.json,
        metadata={"prompt_chars": len(prompt)},
    )
    return 0


def command_review(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_REVIEW_MODEL)
    context = collect_review_context(args)
    prompt, _paths, caveats, metadata = assemble_review_prompt_from_context(
        context,
        max_prompt_chars=args.max_prompt_chars,
    )
    if args.print_prompt:
        if args.json:
            print(json.dumps({"prompt": prompt, "metadata": metadata, "caveats": caveats}, indent=2, sort_keys=True))
            return 0
        print(prompt)
        if caveats:
            print("\n## Assembly Caveats")
            for caveat in caveats:
                print(f"- {caveat}")
        return 0
    if should_run_chunked_review(args, metadata):
        text, caveats, metadata = run_chunked_review(
            args=args,
            context=context,
            model=model,
            base_metadata=metadata,
        )
    else:
        text = post_response(
            base_url=args.base_url,
            model=model,
            prompt=prompt,
            max_output_tokens=args.max_output_tokens,
            timeout=args.timeout,
            token_env=args.gateway_token_env,
            retries=args.retry,
        )
        metadata = {**metadata, "chunked": False}
    print_result(
        mode="review",
        model=model,
        base_url=args.base_url,
        text=text,
        caveats=caveats,
        output_json=args.json,
        metadata=metadata,
    )
    return 0


def command_plan(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_PLAN_MODEL)
    prompt, caveats = assemble_plan_prompt(args)
    if args.print_prompt:
        if args.json:
            print(json.dumps({"prompt": prompt, "caveats": caveats}, indent=2, sort_keys=True))
            return 0
        print(prompt)
        if caveats:
            print("\n## Assembly Caveats")
            for caveat in caveats:
                print(f"- {caveat}")
        return 0
    text = post_response(
        base_url=args.base_url,
        model=model,
        prompt=prompt,
        max_output_tokens=args.max_output_tokens,
        timeout=args.timeout,
        token_env=args.gateway_token_env,
        retries=args.retry,
    )
    print_result(
        mode="plan",
        model=model,
        base_url=args.base_url,
        text=text,
        caveats=caveats,
        output_json=args.json,
        metadata={"prompt_chars": len(prompt)},
    )
    return 0


def command_smoke(args: argparse.Namespace) -> int:
    ok = True
    statuses: dict[str, Any] = {
        "mode": args.mode,
        "cli_available": False,
        "models_reachable": False,
        "sidecar_ready": False,
        "codex_backend_ready": None,
        "blocking": False,
        "checks": [],
    }
    try:
        cmd, _cwd = find_cli()
        statuses["cli_available"] = True
        statuses["checks"].append({"name": "cli", "status": "pass", "detail": " ".join(cmd)})
        if not args.json:
            print(f"[PASS] codex-antigravity CLI: {' '.join(cmd)}")
    except AntiError as exc:
        ok = False
        statuses["checks"].append({"name": "cli", "status": "fail", "detail": str(exc)})
        if not args.json:
            print(f"[FAIL] codex-antigravity CLI: {exc}")

    try:
        ids = fetch_model_ids(args.base_url, timeout=args.timeout, token_env=args.gateway_token_env)
        statuses["models_reachable"] = True
        statuses["checks"].append({"name": "models", "status": "pass", "count": len(ids)})
        if not args.json:
            print(f"[PASS] Gateway /v1/models: {len(ids)} model(s)")
        requested_models = args.model or ["opus", "sonnet"]
        missing_models = []
        for model in [resolve_model(item, default=item) for item in requested_models]:
            if model in ids:
                statuses["checks"].append({"name": "model", "status": "pass", "model": model})
                if not args.json:
                    print(f"[PASS] Model available: {model}")
            else:
                ok = False
                missing_models.append(model)
                statuses["checks"].append({"name": "model", "status": "fail", "model": model})
                if not args.json:
                    print(f"[FAIL] Model missing: {model}")
        statuses["sidecar_ready"] = not missing_models and ok
    except AntiError as exc:
        ok = False
        statuses["checks"].append({"name": "models", "status": "fail", "detail": str(exc)})
        if not args.json:
            print(f"[FAIL] Gateway /v1/models: {exc}")

    should_run_doctor = args.mode in {"full", "codex-backend"} and not args.skip_doctor
    if should_run_doctor:
        if not args.json:
            print("[*] Running codex-antigravity doctor...")
        doctor_args = [
            "doctor",
            "--gateway-base-url",
            args.base_url,
            "--config",
            args.config,
            "--provider",
            args.provider,
        ]
        doctor_rc = run_cli_quiet(doctor_args) if args.json else run_cli(doctor_args)
        if doctor_rc != 0:
            ok = False
            statuses["codex_backend_ready"] = False
            statuses["checks"].append({"name": "doctor", "status": "fail", "detail": "doctor reported hard failures"})
            if not args.json:
                print("[FAIL] doctor reported hard failures")
        else:
            statuses["codex_backend_ready"] = True
            statuses["checks"].append({"name": "doctor", "status": "pass"})
            if not args.json:
                print("[PASS] doctor")
    elif args.mode == "sidecar" and not args.skip_doctor:
        statuses["codex_backend_ready"] = None
        statuses["checks"].append(
            {
                "name": "doctor",
                "status": "skipped",
                "detail": "sidecar mode does not require active Codex backend configuration",
            }
        )
        if not args.json:
            print("[INFO] doctor skipped in sidecar mode; use --mode full to require Codex backend config")

    statuses["blocking"] = not ok
    if args.json:
        print(json.dumps(statuses, indent=2, sort_keys=True))

    return 0 if ok else 1


def command_start(args: argparse.Namespace) -> int:
    base_url = normalize_base_url(args.base_url or f"http://{args.host}:{args.port}/v1")
    if check_gateway(base_url, timeout=args.timeout, token_env=args.gateway_token_env):
        print(f"[PASS] Gateway already reachable at {base_url}")
        return 0

    cmd, cwd = find_cli()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    start_args = [*cmd, "start", "--host", args.host, "--port", str(args.port)]
    if args.allow_remote:
        start_args.append("--allow-remote")
    with LOG_FILE.open("ab") as log_handle:
        proc = subprocess.Popen(
            start_args,
            cwd=cwd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"[*] Started gateway process pid={proc.pid}; log={LOG_FILE}")

    for _ in range(30):
        if proc.poll() is not None:
            print(f"[FAIL] Gateway exited early with code {proc.returncode}; see {LOG_FILE}")
            return 1
        if check_gateway(base_url, timeout=args.timeout, token_env=args.gateway_token_env):
            print(f"[PASS] Gateway reachable at {base_url}")
            return 0
        time.sleep(0.25)
    print(f"[FAIL] Gateway did not become reachable at {base_url}; see {LOG_FILE}")
    return 1


def command_setup_google(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_CONSULT_MODEL)
    cli_args = [
        "setup-google",
        "--accounts",
        str(args.accounts),
        "--config",
        args.config,
        "--model",
        model,
        "--provider",
        args.provider,
        "--provider-name",
        args.provider_name,
        "--port",
        str(args.port),
    ]
    if args.base_url:
        cli_args.extend(["--base-url", args.base_url])
    if args.skip_codex_config:
        cli_args.append("--skip-codex-config")
    if args.skip_doctor:
        cli_args.append("--skip-doctor")
    return run_cli(cli_args)


def command_configure_codex(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_CONSULT_MODEL)
    cli_args = [
        "configure-codex",
        "--write",
        "--config",
        args.config,
        "--model",
        model,
        "--provider",
        args.provider,
        "--provider-name",
        args.provider_name,
        "--base-url",
        args.base_url,
    ]
    return run_cli(cli_args)


def command_doctor(args: argparse.Namespace) -> int:
    cli_args = [
        "doctor",
        "--gateway-base-url",
        args.base_url,
        "--config",
        args.config,
        "--provider",
        args.provider,
    ]
    if args.byok_only:
        cli_args.append("--byok-only")
    return run_cli(cli_args)


def add_gateway_args(
    parser: argparse.ArgumentParser,
    *,
    default_base_url: str | None = DEFAULT_BASE_URL,
    default_timeout: float = 15.0,
) -> None:
    parser.add_argument("--base-url", default=default_base_url, help="Gateway base URL ending in /v1")
    parser.add_argument("--timeout", type=float, default=default_timeout, help="HTTP timeout in seconds")
    parser.add_argument(
        "--gateway-token-env",
        default=DEFAULT_TOKEN_ENV,
        help="Env var containing bearer token for remote gateway access",
    )


def add_codex_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="~/.codex/config.toml", help="Codex config path")
    parser.add_argument("--provider", default="antigravity", help="Codex provider id")
    parser.add_argument("--provider-name", default="Google Antigravity", help="Codex provider display name")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Antigravity Opus/Sonnet sidecar helper for Codex")
    sub = parser.add_subparsers(dest="command", required=True)

    consult = sub.add_parser("consult", aliases=["ask"], help="Ask Antigravity an explicit prompt")
    add_gateway_args(consult, default_timeout=120.0)
    consult.add_argument("--model", default="sonnet", help="opus, sonnet, or full model id")
    consult.add_argument("--prompt", help="Prompt text")
    consult.add_argument("--prompt-file", help="Read prompt text from file")
    consult.add_argument("--max-output-tokens", type=positive_int, default=2048)
    consult.add_argument("--max-prompt-chars", type=int, default=DEFAULT_MAX_PROMPT_CHARS)
    consult.add_argument("--retry", type=int, default=1, help="Retry transient gateway/backend failures")
    consult.add_argument("--json", action="store_true", help="Emit structured JSON output")
    consult.add_argument("prompt_parts", nargs="*", help="Positional prompt text")
    consult.set_defaults(func=command_consult)

    plan = sub.add_parser(
        "plan",
        aliases=["deep-plan", "work-plan"],
        help="Ask Antigravity Opus for a deep autonomous work plan",
    )
    add_gateway_args(plan, default_timeout=120.0)
    plan.add_argument("--model", default="opus", help="opus, sonnet, or full model id")
    plan.add_argument("--prompt", help="Planning goal text")
    plan.add_argument("--prompt-file", help="Read planning goal from file")
    plan.add_argument("--scope", choices=["none", "working-tree", "staged", "files"], default="none")
    plan.add_argument("--file", action="append", help="Add repository file context; repeatable")
    plan.add_argument("--max-output-tokens", type=positive_int, default=6144)
    plan.add_argument("--max-prompt-chars", type=int, default=DEFAULT_MAX_PROMPT_CHARS)
    plan.add_argument("--retry", type=int, default=1, help="Retry transient gateway/backend failures")
    plan.add_argument("--json", action="store_true", help="Emit structured JSON output")
    plan.add_argument("--print-prompt", action="store_true", help="Print assembled prompt without contacting gateway")
    plan.add_argument("prompt_parts", nargs="*", help="Positional planning goal text")
    plan.set_defaults(func=command_plan)

    review = sub.add_parser("review", help="Review git diffs or selected files with Antigravity")
    add_gateway_args(review, default_timeout=120.0)
    review.add_argument("--model", default="opus", help="opus, sonnet, or full model id")
    review.add_argument("--scope", choices=["working-tree", "staged", "files", "diff"], default="working-tree")
    review.add_argument("--base", help="Base ref for --scope diff; uses <base>...HEAD")
    review.add_argument("--changed-files", dest="changed_files_range", help="Git revision range for --scope diff")
    review.add_argument("--file", action="append", help="Limit review to path; repeatable")
    review.add_argument("--files-from", action="append", help="Read review paths from a newline- or NUL-delimited file; use - for stdin")
    review.add_argument("--max-output-tokens", type=positive_int, default=4096)
    review.add_argument("--max-prompt-chars", type=int, default=DEFAULT_MAX_PROMPT_CHARS)
    review.add_argument("--retry", type=int, default=1, help="Retry transient gateway/backend failures")
    review.add_argument(
        "--chunked",
        choices=["auto", "always", "off"],
        default="auto",
        help="Split broad reviews into multiple model calls when needed",
    )
    review.add_argument("--max-review-chunks", type=positive_int, default=8, help="Maximum chunk calls before synthesis")
    review.add_argument("--chunk-output-tokens", type=positive_int, default=2048, help="Max output tokens per chunk review")
    review.add_argument(
        "--max-synthesis-chars",
        type=int,
        default=DEFAULT_MAX_SYNTHESIS_CHARS,
        help="Maximum synthesis prompt chars after chunk outputs; use 0 for unlimited",
    )
    review.add_argument("--json", action="store_true", help="Emit structured JSON output")
    review.add_argument("--print-prompt", action="store_true", help="Print assembled prompt without contacting gateway")
    review.set_defaults(func=command_review)

    smoke = sub.add_parser("smoke", help="Check CLI, gateway, models, and doctor readiness")
    add_gateway_args(smoke)
    add_codex_config_args(smoke)
    smoke.add_argument(
        "--mode",
        choices=["sidecar", "full", "codex-backend"],
        default="sidecar",
        help="sidecar checks CLI/gateway/models; full/codex-backend also require doctor/Codex config",
    )
    smoke.add_argument("--model", action="append", help="Required model alias/id; defaults to opus and sonnet")
    smoke.add_argument("--skip-doctor", action="store_true")
    smoke.add_argument("--json", action="store_true", help="Emit structured JSON readiness output")
    smoke.set_defaults(func=command_smoke)

    start = sub.add_parser("start", help="Start gateway in background if it is not reachable")
    add_gateway_args(start, default_base_url=None, default_timeout=2.0)
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=51122)
    start.add_argument("--allow-remote", action="store_true")
    start.set_defaults(func=command_start)

    setup = sub.add_parser("setup-google", help="Run guided Google Antigravity setup")
    setup.add_argument("--accounts", type=int, default=1)
    setup.add_argument("--model", default="sonnet")
    setup.add_argument("--port", type=int, default=51122)
    setup.add_argument("--base-url")
    setup.add_argument("--skip-codex-config", action="store_true")
    setup.add_argument("--skip-doctor", action="store_true")
    add_codex_config_args(setup)
    setup.set_defaults(func=command_setup_google)

    configure = sub.add_parser("configure-codex", help="Write Codex provider config for Antigravity")
    configure.add_argument("--model", default="sonnet")
    add_gateway_args(configure)
    add_codex_config_args(configure)
    configure.set_defaults(func=command_configure_codex)

    doctor = sub.add_parser("doctor", help="Run codex-antigravity doctor")
    add_gateway_args(doctor)
    add_codex_config_args(doctor)
    doctor.add_argument("--byok-only", action="store_true")
    doctor.set_defaults(func=command_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "base_url") and args.base_url is not None:
        args.base_url = normalize_base_url(args.base_url)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        eprint("Interrupted")
        return 130
    except AntiError as exc:
        eprint(f"[anti] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
