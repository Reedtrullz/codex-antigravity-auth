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
    status, decoded = request_json(
        "POST",
        f"{normalize_base_url(base_url)}/responses",
        payload=payload,
        timeout=timeout,
        token_env=token_env,
    )
    if status != 200:
        detail = decoded.get("detail") or decoded.get("error") or decoded
        raise AntiError(f"/v1/responses returned HTTP {status}: {detail}")
    return extract_response_text(decoded)


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


def changed_paths(root: Path, scope: str, selected: list[str]) -> tuple[list[str], list[str]]:
    if selected:
        return filter_paths(selected, root=root)
    if scope == "staged":
        raw = run_git(root, ["diff", "--cached", "--name-only", "--diff-filter=ACMRT"])
    elif scope == "working-tree":
        raw = run_git(root, ["diff", "HEAD", "--name-only", "--diff-filter=ACMRT"])
    elif scope == "files":
        raise AntiError("--scope files requires at least one --file")
    else:
        raise AntiError(f"unsupported review scope: {scope}")
    return filter_paths(raw.splitlines(), root=root)


def diff_for_paths(root: Path, scope: str, paths: list[str]) -> str:
    if not paths or scope == "files":
        return ""
    if scope == "staged":
        return run_git(root, ["diff", "--cached", "--no-ext-diff", "--", *paths], check=False)
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
    if len(raw) > MAX_FILE_BYTES:
        raw = raw[:MAX_FILE_BYTES]
        note = f"{rel_path}: truncated to {MAX_FILE_BYTES} bytes"
    else:
        note = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "", f"{rel_path}: non-UTF-8 file skipped"
    return text, note


def apply_prompt_limit(prompt: str, max_prompt_chars: int, caveats: list[str]) -> str:
    if max_prompt_chars > 0 and len(prompt) > max_prompt_chars:
        caveats.append(f"Prompt truncated to {max_prompt_chars} characters")
        return prompt[:max_prompt_chars]
    return prompt


def assemble_review_prompt(args: argparse.Namespace) -> tuple[str, list[str], list[str]]:
    root = find_repo_root(Path.cwd())
    if root is None:
        if args.scope != "files":
            raise AntiError("review requires a git repository unless --scope files is used")
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

    prompt = "\n".join(
        part
        for part in [
            "You are an Antigravity sidecar reviewer for a Codex coding session.",
            "Review independently. Lead with concrete defects, regressions, security risks, install/usability problems, or missing tests. Avoid speculative style comments.",
            "Use file paths and precise behavior references when possible. If you find no issues, say so and list residual verification caveats.",
            f"Review scope: {scope_line}.",
            "\n## Git Diff\n```diff\n" + diff + "\n```" if diff.strip() else "",
            "\n## File Contents\n" + "\n\n".join(file_blocks) if file_blocks else "",
        ]
        if part
    )

    if not diff.strip() and not file_blocks:
        prompt += "\n\nNo diff or file content was available in the requested scope. Explain that limitation."

    caveats = []
    if excluded:
        caveats.append("Excluded sensitive/cache/binary-looking paths: " + ", ".join(excluded[:20]))
    if notes:
        caveats.extend(notes)
    prompt = apply_prompt_limit(prompt, args.max_prompt_chars, caveats)
    return prompt, paths, caveats


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


def print_result(*, mode: str, model: str, base_url: str, text: str, caveats: list[str] | None = None) -> None:
    print(f"## Antigravity {mode} ({model})")
    print(f"- Gateway: {base_url}")
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
    )
    print_result(mode="consult", model=model, base_url=args.base_url, text=text, caveats=caveats)
    return 0


def command_review(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_REVIEW_MODEL)
    prompt, _paths, caveats = assemble_review_prompt(args)
    if args.print_prompt:
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
    )
    print_result(mode="review", model=model, base_url=args.base_url, text=text, caveats=caveats)
    return 0


def command_plan(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_PLAN_MODEL)
    prompt, caveats = assemble_plan_prompt(args)
    if args.print_prompt:
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
    )
    print_result(mode="plan", model=model, base_url=args.base_url, text=text, caveats=caveats)
    return 0


def command_smoke(args: argparse.Namespace) -> int:
    ok = True
    try:
        cmd, _cwd = find_cli()
        print(f"[PASS] codex-antigravity CLI: {' '.join(cmd)}")
    except AntiError as exc:
        ok = False
        print(f"[FAIL] codex-antigravity CLI: {exc}")

    try:
        ids = fetch_model_ids(args.base_url, timeout=args.timeout, token_env=args.gateway_token_env)
        print(f"[PASS] Gateway /v1/models: {len(ids)} model(s)")
        requested_models = args.model or ["opus", "sonnet"]
        for model in [resolve_model(item, default=item) for item in requested_models]:
            if model in ids:
                print(f"[PASS] Model available: {model}")
            else:
                ok = False
                print(f"[FAIL] Model missing: {model}")
    except AntiError as exc:
        ok = False
        print(f"[FAIL] Gateway /v1/models: {exc}")

    if not args.skip_doctor:
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
        if run_cli(doctor_args) != 0:
            ok = False
            print("[FAIL] doctor reported hard failures")
        else:
            print("[PASS] doctor")

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
    consult.add_argument("--max-output-tokens", type=int, default=2048)
    consult.add_argument("--max-prompt-chars", type=int, default=DEFAULT_MAX_PROMPT_CHARS)
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
    plan.add_argument("--max-output-tokens", type=int, default=6144)
    plan.add_argument("--max-prompt-chars", type=int, default=DEFAULT_MAX_PROMPT_CHARS)
    plan.add_argument("--print-prompt", action="store_true", help="Print assembled prompt without contacting gateway")
    plan.add_argument("prompt_parts", nargs="*", help="Positional planning goal text")
    plan.set_defaults(func=command_plan)

    review = sub.add_parser("review", help="Review git diffs or selected files with Antigravity")
    add_gateway_args(review, default_timeout=120.0)
    review.add_argument("--model", default="opus", help="opus, sonnet, or full model id")
    review.add_argument("--scope", choices=["working-tree", "staged", "files"], default="working-tree")
    review.add_argument("--file", action="append", help="Limit review to path; repeatable")
    review.add_argument("--max-output-tokens", type=int, default=4096)
    review.add_argument("--max-prompt-chars", type=int, default=DEFAULT_MAX_PROMPT_CHARS)
    review.add_argument("--print-prompt", action="store_true", help="Print assembled prompt without contacting gateway")
    review.set_defaults(func=command_review)

    smoke = sub.add_parser("smoke", help="Check CLI, gateway, models, and doctor readiness")
    add_gateway_args(smoke)
    add_codex_config_args(smoke)
    smoke.add_argument("--model", action="append", help="Required model alias/id; defaults to opus and sonnet")
    smoke.add_argument("--skip-doctor", action="store_true")
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
