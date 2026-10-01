"""Non-secret setup profiles and receipts for explicitly restorable local changes."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

from .namespaces import gateway_home, client_config_path, client_skills_path
from .secure_store import SecureStore, file_lock
from .skills.anti.scripts.anti_lib.file_protection import (
    ensure_private_directory, verify_regular_descriptor, _directory,
)

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RUN = re.compile(r"^[0-9a-f]{32}$")
MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 10000
STAGES = ("credentials", "login", "config", "skill", "gateway", "readiness")


class SetupError(RuntimeError):
    pass


def _cli():
    from . import cli
    return cli


def _json(path: Path, value):
    SecureStore().atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_file(path: Path, limit=MAX_BYTES):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > limit:
        raise SetupError("Unsupported or oversized setup file; preserve it for manual inspection")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        verify_regular_descriptor(stream.fileno(), path)
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise SetupError("Setup file exceeds the operation size limit")
    return value


def _load_json(path, limit=65536):
    try:
        value = _read_file(path, limit)
        if value is None:
            raise SetupError("Requested setup state does not exist")
        result = json.loads(value)
        if not isinstance(result, dict) or type(result.get("schemaVersion")) is not int or result["schemaVersion"] != 1:
            raise SetupError("Unsupported setup state version; original state was preserved")
        return result
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SetupError("Malformed setup state; original state was preserved") from exc


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _tree(path):
    if path.is_symlink():
        raise SetupError("Setup refuses symlinked skill trees")
    if not path.exists():
        return None
    if not path.is_dir():
        raise SetupError("Skill target is not a directory")
    _directory(path, protect=False)
    result = {}
    total = 0
    for item in sorted(path.rglob("*")):
        if len(result) >= MAX_FILES:
            raise SetupError("Skill tree exceeds the setup file-count limit")
        info = item.lstat()
        relative = item.relative_to(path).as_posix()
        if stat.S_ISDIR(info.st_mode):
            _directory(item, protect=False)
            result[relative] = None
        elif stat.S_ISREG(info.st_mode):
            raw = _read_file(item, MAX_BYTES - total)
            total += len(raw)
            result[relative] = (raw, bool(info.st_mode & 0o100))
        else:
            raise SetupError("Skill snapshot contains a symlink or unsupported entry")
    return result


def _tree_hash(tree):
    if tree is None:
        return None
    digest = hashlib.sha256()
    for relative, value in sorted(tree.items()):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
        if value is None:
            digest.update(b"directory")
        else:
            digest.update(b"file" + bytes([value[1]]) + len(value[0]).to_bytes(8, "big") + value[0])
    return digest.hexdigest()


def _snapshot(target, kind):
    if kind == "config":
        raw = _read_file(target)
        return raw, {"exists": raw is not None, "sha256": _hash(raw) if raw is not None else None}
    tree = _tree(target)
    return tree, {"exists": tree is not None, "sha256": _tree_hash(tree)}


def _materialize(path, tree):
    ensure_private_directory(path)
    for relative, value in sorted(tree.items()):
        target = path / relative
        if value is None:
            ensure_private_directory(target)
        else:
            ensure_private_directory(target.parent)
            SecureStore()._atomic_write_bytes_unlocked(target, value[0], mode=0o700 if value[1] else 0o600)
    for directory in sorted((item for item in path.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        SecureStore._fsync_directory(directory)
    SecureStore._fsync_directory(path)


def profile_root():
    return gateway_home() / "setup-profiles"


def receipts_root():
    return gateway_home() / "setup-receipts"


def settings_from_args(args):
    cli = _cli()
    provider, name = cli.unified_provider_defaults(args)
    settings = {
        "model": cli.validate_codex_model_id(args.model),
        "provider": cli.validate_codex_provider_id(provider),
        "provider_name": cli.validate_codex_provider_name(name),
        "base_url": cli.validate_http_base_url(args.base_url or cli.DEFAULT_CODEX_BASE_URL, label="Gateway base URL"),
        "unified_model_picker": bool(getattr(args, "unified_model_picker", False)),
    }
    for value in settings.values():
        if isinstance(value, str) and (len(value) > 2048 or cli.redact_secret_text(value) != value):
            raise SetupError("Profiles accept only non-secret settings; use environment-variable references")
    return settings


def validate_profile(value):
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise SetupError("Unsupported profile version")
    if set(value) != {"schemaVersion", "name", "settings", "secretReferences"} or not isinstance(value.get("name"), str) or not NAME.fullmatch(value["name"]):
        raise SetupError("Invalid profile shape")
    if _cli().redact_secret_text(value["name"]) != value["name"]:
        raise SetupError("Profile name must not contain credentials")
    settings = value.get("settings")
    if not isinstance(settings, dict) or set(settings) != {"model", "provider", "provider_name", "base_url", "unified_model_picker"}:
        raise SetupError("Invalid profile settings")
    if type(settings["unified_model_picker"]) is not bool or any(not isinstance(settings[key], str) for key in settings if key != "unified_model_picker"):
        raise SetupError("Invalid profile setting types")
    import argparse
    normalized = settings_from_args(argparse.Namespace(**settings))
    references = value.get("secretReferences")
    if not isinstance(references, dict) or set(references) != {"gatewayTokenEnv"}:
        raise SetupError("Profiles accept only named secret references")
    reference = references["gatewayTokenEnv"]
    if reference is not None and (not isinstance(reference, str) or len(reference) > 128 or not ENV.fullmatch(reference)):
        raise SetupError("Secret references must be environment-variable names")
    if normalized != settings:
        raise SetupError("Profile settings are not canonical; recreate the profile")
    return value


def create_profile(args):
    if not NAME.fullmatch(args.name):
        raise SetupError("Profile name must contain only letters, numbers, '_' or '-'")
    value = {"schemaVersion": 1, "name": args.name, "settings": settings_from_args(args),
             "secretReferences": {"gatewayTokenEnv": getattr(args, "gateway_token_env", None)}}
    validate_profile(value)
    path = profile_root() / f"{args.name}.json"
    if path.exists() or path.is_symlink():
        raise SetupError("Profile already exists; choose a new name")
    if args.write:
        with file_lock(path):
            if path.exists() or path.is_symlink():
                raise SetupError("Profile already exists; choose a new name")
            _json(path, value)
    return {"schemaVersion": 1, "ok": True, "write": bool(args.write), "profile": value, "path": str(path)}


def load_profile(name):
    if not NAME.fullmatch(name):
        raise SetupError("Invalid profile name")
    value = validate_profile(_load_json(profile_root() / f"{name}.json"))
    if value["name"] != name:
        raise SetupError("Profile identity does not match its file")
    return value


def setup_plan(args, *, profile=None):
    cli = _cli()
    if profile is None:
        import argparse
        copied = argparse.Namespace(**vars(args))
        copied.base_url = cli.setup_effective_base_url(args)
        settings = settings_from_args(copied)
    else:
        settings = profile["settings"]
    config_entry = client_config_path(args.config).absolute()
    config = config_entry.resolve()
    raw = _read_file(config)
    try:
        text = raw.decode("utf-8") if raw is not None else ""
    except UnicodeError as exc:
        raise SetupError("Config must be UTF-8 before planning setup") from exc
    activate = bool(getattr(args, "activate", False))
    if profile is None:
        preview = cli.merge_codex_config(text, model=settings["model"], provider_id=settings["provider"], provider_name=settings["provider_name"],
                                        base_url=settings["base_url"], activate=activate)
    else:
        from .codex_config import merge_profile_config
        preview = merge_profile_config(text, settings=settings, token_env=profile["secretReferences"]["gatewayTokenEnv"], activate=activate)
    skill = (client_skills_path(getattr(args, "skill_dir", cli.DEFAULT_CODEX_SKILLS_DIR)) / cli.BUNDLED_CODEX_SKILL_NAME).absolute()
    repair = bool(getattr(args, "repair", False))
    local_only = profile is not None
    flags = {"credentials": not repair and not local_only, "login": not repair and not local_only,
             "config": True, "skill": bool(getattr(args, "install_skill", False)) and not repair,
             "gateway": bool(getattr(args, "start", False)) and not repair and not local_only,
             "readiness": not local_only}
    return {"schemaVersion": 1, "ok": True, "readOnly": True, "settings": settings,
            "activateDefault": activate, "configWouldChange": preview != text,
            "secretReferences": dict(profile["secretReferences"]) if profile else {},
            "ownedConfigFields": ["name", "base_url", "wire_api"] + (["env_key"] if profile else []),
            "config": str(config_entry), "configTarget": str(config), "configBeforeSha256": _hash(raw) if raw is not None else None,
            "skill": str(skill), "profile": profile["name"] if profile else None,
            "prerequisites": [{"name": "route_and_credentials", "status": "not_checked", "condition": "credential/login stages run only for routes that need them"},
                              {"name": "file_ownership", "status": "checked_during_apply"}],
            "stages": [{"id": key, "requested": flags[key], "state": "pending" if flags[key] else "skipped", "restorable": key in {"config", "skill"}} for key in STAGES],
            "gatewayPort": int(getattr(args, "port", 51122)), "credentialsAndServicesRestorable": False}


class SetupJournal:
    def __init__(self, plan):
        self.id = uuid.uuid4().hex
        self.directory = receipts_root() / self.id
        ensure_private_directory(self.directory)
        self.path = self.directory / "receipt.json"
        self.data = {"schemaVersion": 1, "id": self.id, "createdAt": int(time.time()), "state": "running", "plan": plan,
                     "stages": [dict(stage) for stage in plan["stages"]], "nextSteps": []}
        self.persist()

    def persist(self):
        _json(self.path, self.data)

    def run(self, stage_id, operation, *args, **kwargs):
        stage = next(stage for stage in self.data["stages"] if stage["id"] == stage_id)
        target = Path(self.data["plan"]["configTarget"] if stage_id == "config" else self.data["plan"]["skill"]) if stage_id in {"config", "skill"} else None
        lock = file_lock(target) if target else contextlib.nullcontext()
        began = False
        try:
            with lock:
                stage["state"] = "running"
                stage["operationStarted"] = False
                if target:
                    if stage_id == "config" and Path(self.data["plan"]["config"]).resolve() != target:
                        raise SetupError("Config target changed since planning")
                    before, stage["before"] = _snapshot(target, stage_id)
                    if stage_id == "config" and stage["before"]["sha256"] != self.data["plan"]["configBeforeSha256"]:
                        raise SetupError("Config changed since planning; create a fresh plan")
                    stage["target"] = str(target)
                    stage["backup"] = f"{stage_id}-before"
                    if before is not None:
                        if stage_id == "config":
                            SecureStore().atomic_write_bytes(self.directory / stage["backup"], before)
                        else:
                            _materialize(self.directory / stage["backup"], before)
                        _, backed_up = _snapshot(self.directory / stage["backup"], stage_id)
                        if backed_up != stage["before"]:
                            raise SetupError("Setup backup validation failed before mutation")
                    _, still_current = _snapshot(target, stage_id)
                    if still_current != stage["before"]:
                        raise SetupError("Target changed during backup; setup mutation refused")
                self.persist()
                stage["operationStarted"] = True
                self.persist()
                began = True
                try:
                    result = operation(*args, **kwargs)
                except BaseException:
                    if target:
                        try:
                            _, stage["after"] = _snapshot(target, stage_id)
                        except Exception:
                            stage["after"] = None
                    raise
                if target:
                    _, stage["after"] = _snapshot(target, stage_id)
                stage["state"] = "completed"
                if isinstance(result, dict) and type(result.get("ok")) is bool:
                    stage["checkOk"] = result["ok"]
                self.persist()
                return result
        except BaseException as exc:
            stage["state"], stage["errorClass"] = "failed", type(exc).__name__
            stage["operationStarted"] = began
            self.persist()
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise SetupError(f"Setup stage {stage_id} failed; inspect receipt {self.id}. No automatic rollback was attempted") from None

    def finish(self, *, ok):
        self.data["state"] = "completed" if ok else "failed"
        if ok:
            for stage in self.data["stages"]:
                if stage["state"] == "pending":
                    stage["state"] = "skipped"
        self.data["nextSteps"] = [f"codex-antigravity setup-history show {self.id}"]
        if any(stage["id"] == "gateway" and stage["state"] in {"completed", "failed"} for stage in self.data["stages"]):
            self.data["nextSteps"].append(f"codex-antigravity status --port {self.data['plan']['gatewayPort']}")
        restorable = [stage["id"] for stage in self.data["stages"] if stage["id"] in {"config", "skill"}
                      and stage.get("before") != stage.get("after") and stage.get("before") and stage.get("after")]
        if restorable:
            choices = " ".join(f"--stage {name}" for name in restorable)
            self.data["nextSteps"].append(f"codex-antigravity setup-history restore {self.id} {choices}")
        self.persist()


def stage(args, name, operation, *positional, **kwargs):
    journal = getattr(args, "_setup_journal", None)
    return journal.run(name, operation, *positional, **kwargs) if journal else operation(*positional, **kwargs)


def load_receipt(run_id):
    if not RUN.fullmatch(run_id):
        raise SetupError("Invalid setup receipt id")
    path = receipts_root() / run_id / "receipt.json"
    value = _load_json(path, 1024 * 1024)
    if value.get("id") != run_id or not isinstance(value.get("state"), str) or value["state"] not in {"running", "completed", "failed"}:
        raise SetupError("Invalid setup receipt identity or state")
    rows = value.get("stages")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows) or [row.get("id") for row in rows] != list(STAGES):
        raise SetupError("Unsupported setup stages")
    plan = value.get("plan")
    if not isinstance(plan, dict) or any(not isinstance(plan.get(key), str) or not Path(plan[key]).is_absolute() for key in ("config", "configTarget", "skill")):
        raise SetupError("Invalid setup plan")
    for row in rows:
        if not isinstance(row.get("state"), str) or row["state"] not in {"pending", "skipped", "running", "completed", "failed", "restoring", "restored"}:
            raise SetupError("Unsupported setup stage state")
        if "operationStarted" in row and type(row["operationStarted"]) is not bool:
            raise SetupError("Invalid setup operation state")
        if row.get("operationStarted") is False and row.get("after") is not None:
            raise SetupError("Unstarted setup stage contains an inconsistent result")
        for key in ("before", "after"):
            meta = row.get(key)
            if meta is not None:
                if not isinstance(meta, dict) or set(meta) != {"exists", "sha256"} or type(meta["exists"]) is not bool:
                    raise SetupError("Invalid setup snapshot metadata")
                if (meta["exists"] and (not isinstance(meta["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"]))) or (not meta["exists"] and meta["sha256"] is not None):
                    raise SetupError("Invalid setup snapshot identity")
        if row["id"] in {"config", "skill"} and "target" in row:
            expected = plan["configTarget"] if row["id"] == "config" else plan["skill"]
            if row["target"] != expected or row.get("backup") != f"{row['id']}-before":
                raise SetupError("Setup snapshot references conflict with its plan")
    return path, value


def restore_receipt(run_id, selected, *, write=False):
    path, _receipt = load_receipt(run_id)
    with file_lock(path) if write else contextlib.nullcontext():
        return _restore_receipt(run_id, selected, write=write)


def _restore_receipt(run_id, selected, *, write=False):
    path, receipt = load_receipt(run_id)
    if not selected or set(selected) - {"config", "skill"}:
        raise SetupError("Select config and/or skill explicitly; credentials and services cannot be restored")
    if receipt["state"] == "running":
        raise SetupError("Setup is still running or its outcome is unknown; inspect backups manually")
    results = []
    for name in dict.fromkeys(selected):
        row = next(row for row in receipt["stages"] if row["id"] == name)
        if row.get("operationStarted") is False:
            results.append({"stage": name, "status": "not_applied"})
            continue
        if row["state"] == "restoring":
            raise SetupError("An earlier restore outcome is uncertain; inspect retained paths manually")
        if not row.get("after") or not row.get("before"):
            if row["state"] in {"pending", "skipped"}:
                results.append({"stage": name, "status": "not_applied"})
                continue
            raise SetupError("Setup stage outcome is uncertain; inspect its retained backup manually")
        target = Path(row["target"])
        if not target.is_absolute() or row.get("backup") != f"{name}-before":
            raise SetupError("Invalid restore target or backup reference")
        backup = path.parent / row["backup"]
        context = file_lock(target) if write else contextlib.nullcontext()
        with context:
            if name == "config" and Path(receipt["plan"]["config"]).resolve() != target:
                raise SetupError("Config path changed; automatic restoration refused")
            current, current_meta = _snapshot(target, name)
            expected_current = row["before"] if row["state"] == "restored" else row["after"]
            if current_meta != expected_current:
                raise SetupError("Target has drifted since setup/restoration; automatic restoration refused")
            if row["state"] == "restored" or row["before"] == row["after"]:
                results.append({"stage": name, "status": "unchanged"})
                continue
            original, original_meta = _snapshot(backup, name)
            if original_meta != row["before"]:
                raise SetupError("Original backup is missing or changed; restoration refused")
            if write:
                retained = path.parent / "config-before-restore" if name == "config" else target.with_name(f".{target.name}.setup-restore-{run_id}")
                if retained.exists() or retained.is_symlink():
                    raise SetupError(f"Restore recovery path already exists; preserve and inspect {retained}")
                row["restorePaths"] = {"target": str(target), "originalBackup": str(backup), "displaced": str(retained)}
                row["state"] = "restoring"
                _json(path, receipt)
                if name == "config":
                    if current is not None:
                        SecureStore().atomic_write_bytes(path.parent / "config-before-restore", current)
                    if original is None:
                        target.unlink()
                    else:
                        SecureStore().atomic_write_bytes(target, original)
                else:
                    with tempfile.TemporaryDirectory(prefix=f".{target.name}.restore-", dir=target.parent) as temporary:
                        staged = Path(temporary) / "payload"
                        if original is not None:
                            _materialize(staged, original)
                        moved = False
                        try:
                            if current is not None:
                                target.rename(retained)
                                moved = True
                            if original is not None:
                                staged.rename(target)
                        except BaseException:
                            if moved and not target.exists():
                                retained.rename(target)
                            raise
                    row["retainedAfterRestore"] = str(retained) if moved else None
                SecureStore._fsync_directory(target.parent)
                _, restored_meta = _snapshot(target, name)
                if restored_meta != row["before"]:
                    raise SetupError("Restored target could not be verified; retained backups require manual inspection")
                row["state"] = "restored"
                _json(path, receipt)
            results.append({"stage": name, "status": "restored" if write else "would_restore"})
    return {"schemaVersion": 1, "ok": True, "write": write, "receipt": run_id, "stages": results}


def apply_profile(args):
    profile = load_profile(args.name)
    plan = setup_plan(args, profile=profile)
    if not args.write:
        return {"schemaVersion": 1, "ok": True, "write": False, "plan": plan}
    journal = SetupJournal(plan)
    from .codex_config import merge_profile_config
    settings = profile["settings"]
    def configure():
        target = Path(plan["configTarget"])
        raw = _read_file(target)
        merged = merge_profile_config(raw.decode("utf-8") if raw is not None else "", settings=settings,
                                      token_env=profile["secretReferences"]["gatewayTokenEnv"], activate=args.activate)
        SecureStore().atomic_write_text(target, merged)
    try:
        journal.run("config", configure)
        if args.install_skill:
            journal.run("skill", _cli().install_codex_skill, client_skills_path(args.skill_dir), force=args.force)
    except BaseException:
        journal.finish(ok=False)
        raise
    journal.finish(ok=True)
    next_steps = ["codex-antigravity doctor --codex-ready"]
    if settings["unified_model_picker"]:
        next_steps.insert(0, "codex-antigravity start --unified-model-picker")
    return {"schemaVersion": 1, "ok": True, "write": True, "receipt": journal.data,
            "runtimeChanged": False, "nextSteps": next_steps}


def add_parsers(subparsers):
    cli = _cli()
    profiles = subparsers.add_parser("profiles", help="Plan, create and apply named non-secret local settings")
    actions = profiles.add_subparsers(dest="profile_action", required=True)
    create = actions.add_parser("create")
    create.add_argument("name")
    create.add_argument("--model", default=cli.DEFAULT_CODEX_MODEL_ID)
    create.add_argument("--provider", default=cli.DEFAULT_CODEX_PROVIDER_ID)
    create.add_argument("--provider-name", default=cli.DEFAULT_CODEX_PROVIDER_NAME)
    create.add_argument("--base-url", default=cli.DEFAULT_CODEX_BASE_URL)
    create.add_argument("--unified-model-picker", action="store_true")
    create.add_argument("--gateway-token-env", help="Environment-variable name only; the value is never read")
    create.add_argument("--write", action="store_true", help="Create the profile; default is a no-write plan")
    actions.add_parser("list")
    show = actions.add_parser("show")
    show.add_argument("name")
    apply = actions.add_parser("apply")
    apply.add_argument("name")
    apply.add_argument("--config", default="~/.codex/config.toml")
    apply.add_argument("--skill-dir", default=cli.DEFAULT_CODEX_SKILLS_DIR)
    apply.add_argument("--install-skill", action="store_true")
    apply.add_argument("--force", action="store_true")
    apply.add_argument("--activate", action="store_true", help="Explicitly select this model/provider as the active default")
    apply.add_argument("--write", action="store_true", help="Apply settings; default is a no-write plan")
    history = subparsers.add_parser("setup-history", help="Inspect setup receipts or explicitly restore config/skill changes")
    actions = history.add_subparsers(dest="history_action", required=True)
    actions.add_parser("list")
    show = actions.add_parser("show")
    show.add_argument("id")
    restore = actions.add_parser("restore")
    restore.add_argument("id")
    restore.add_argument("--stage", choices=["config", "skill"], action="append", required=True)
    restore.add_argument("--write", action="store_true", help="Restore selected owned stages; default is a no-write plan")


def run_command(args):
    try:
        if args.command == "profiles":
            if args.profile_action == "create":
                result = create_profile(args)
            elif args.profile_action == "show":
                result = {"schemaVersion": 1, "ok": True, "profile": load_profile(args.name)}
            elif args.profile_action == "list":
                result = {"schemaVersion": 1, "ok": True, "profiles": [load_profile(path.stem) for path in sorted(profile_root().glob("*.json"))]}
            else:
                result = apply_profile(args)
        elif args.history_action == "restore":
            result = restore_receipt(args.id, args.stage, write=args.write)
        elif args.history_action == "show":
            result = {"schemaVersion": 1, "ok": True, "receipt": load_receipt(args.id)[1]}
        else:
            result = {"schemaVersion": 1, "ok": True, "receipts": [load_receipt(path.parent.name)[1] for path in sorted(receipts_root().glob("*/receipt.json"))]}
    except (SetupError, ValueError, OSError) as exc:
        print(json.dumps({"schemaVersion": 1, "ok": False, "error": _cli().redact_secret_text(str(exc))}))
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))
    return result
