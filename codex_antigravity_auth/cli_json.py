"""Version 1 operational result envelope and single-document stdout boundary."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import sys

from . import cli
from .redaction import redact_secrets
from .support_bundle import collect_bundle, export_bundle, number

MAX_CAPTURE_CHARS = 2 * 1024 * 1024


class JSONUsageError(ValueError):
    pass


class _Capture(io.StringIO):
    def write(self, value):
        if self.tell() + len(value) > MAX_CAPTURE_CHARS:
            raise ValueError("command output limit")
        return super().write(value)


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        if "--json" in sys.argv or "support-bundle" in sys.argv:
            print(json.dumps(envelope("arguments", None, errors=["invalid_arguments"], exit_code=2)))
            raise SystemExit(2)
        super().error(message)


def envelope(command, data, *, warnings=(), errors=(), exit_code=None):
    code = (1 if errors else 0) if exit_code is None else exit_code
    return {"schemaVersion": 1, "command": command,
            "status": "failed" if errors else "degraded" if warnings else "ready",
            "ok": not bool(errors), "exitCode": code,
            "warnings": list(warnings), "errors": list(errors), "data": data}


def _readiness(args):
    return cli.codex_ready_report(
        config=args.config, provider_id=args.provider, expected_base_url=args.gateway_base_url,
        gateway_timeout=args.gateway_timeout, gateway_token_env=args.gateway_token_env,
        live=args.live, live_model=args.live_model, live_timeout=args.live_timeout,
        include_version_check=False,
    )


def _collect(args):
    command = args.command
    if command == "support-bundle":
        if args.write and not args.output:
            raise JSONUsageError("export path required")
        from .observability import _parse_since_seconds
        try:
            window = _parse_since_seconds(args.since)
            if window is not None and number(window) is None:
                raise ValueError("window limit")
            if len(args.request_id) > 20 or any(not 1 <= len(value) <= 256 for value in args.request_id):
                raise ValueError("selection limit")
        except ValueError as exc:
            raise JSONUsageError("invalid support selection") from exc
        bundle = collect_bundle(config=args.config, since=args.since, request_ids=args.request_id)
        if args.write:
            export_bundle(bundle, args.output)
        return {"mode": "export" if args.write else "dry_run", "written": bool(args.write), "bundle": bundle}
    if command == "doctor":
        return _readiness(args)
    if command == "setup":
        # Existing setup JSON remains an inspection. It must not introduce an
        # update lookup/cache write just because it is machine-readable.
        return cli.run_setup(args)
    if command == "status":
        return cli.run_gateway_status(args)
    if command == "service":
        return cli.run_service_command(args)
    if command == "logs":
        if args.follow:
            raise JSONUsageError("JSON follow is not a single result")
        if args.logs_action == "summary":
            from .observability import _parse_since_seconds
            try:
                window = _parse_since_seconds(args.since)
                if window is not None and number(window) is None:
                    raise ValueError("window limit")
            except ValueError as exc:
                raise JSONUsageError("invalid summary window") from exc
            records = list(cli.iter_request_records(max_bytes=2 * 1024 * 1024, max_records=2000))
            return cli.request_log_summary(since=args.since, records=records)
        if args.logs_action == "clean":
            return {"removed": cli.clean_request_logs()}
        if not 0 <= args.tail <= 2000:
            raise JSONUsageError("tail limit")
        return {"records": list(cli.iter_request_records(tail=args.tail, max_bytes=2 * 1024 * 1024, max_records=2000))}
    if command == "accounts":
        accounts = cli.load_accounts_read_only().get("accounts", [])
        return {"accountCount": len(accounts), "accounts": [
            {"accountIndex": index, "enabled": account.get("isDisabled") is not True,
             "refreshPresent": bool(account.get("refreshToken")), "accessPresent": bool(account.get("accessToken"))}
            for index, account in enumerate(accounts) if isinstance(account, dict)]}
    if command == "models":
        return {"models": cli.native_model_catalog(strict_overlays=True),
                "overlays": [model.id for model in cli.load_model_overlays(strict=True)]}
    if command == "provider":
        if args.provider_command == "presets":
            return {"providers": cli.PROVIDER_PRESETS}
        # Report structured capabilities/counts without exporting provider keys,
        # custom URLs, names or extra header values from the store.
        providers = cli.load_provider_config_read_only().get("providers", {})
        return {"providerCount": len(providers), "providers": [
            {"providerIndex": index, "modelCount": len(provider.get("models", [])),
             "kind": provider.get("kind") if provider.get("kind") in {"openai_chat", "openai_responses"} else "unknown",
             "storedKeyPresent": bool(provider.get("apiKey")), "keyReferencePresent": bool(provider.get("apiKeyEnv"))}
            for index, provider in enumerate(providers.values()) if isinstance(provider, dict)]}
    raise JSONUsageError("unsupported JSON operation")


def _outcome(command, data):
    warnings, errors = [], []
    if type(data) is dict:
        checks = data.get("checks", [])
        for check in checks if isinstance(checks, list) else []:
            if isinstance(check, dict) and check.get("status") in {"warn", "fail"}:
                (warnings if check["status"] == "warn" else errors).append("diagnostic_warning" if check["status"] == "warn" else "diagnostic_failed")
        if data.get("ok") is False and not errors:
            errors.append("operation_failed")
        if command == "status" and not data.get("reachable"):
            warnings.append("gateway_unreachable")
        if command == "service" and not data.get("gateway", {}).get("reachable"):
            warnings.append("gateway_unreachable")
        if command == "logs":
            if (data.get("requested_window_incomplete") or data.get("malformed_records") or data.get("omitted_records")
                    or any(row.get("status") in {"malformed", "log_gap"} for row in data.get("records", []) if isinstance(row, dict))):
                warnings.append("history_incomplete")
            elif "requested_window_incomplete" in data and data["requested_window_incomplete"] is None:
                warnings.append("history_coverage_unknown")
        if command == "support-bundle":
            warnings.extend(data["bundle"]["warnings"])
    return sorted(set(warnings)), sorted(set(errors))


def run(args):
    command = args.command
    action = next((getattr(args, name) for name in ("service_command", "logs_action", "accounts_action", "models_command", "provider_command")
                   if getattr(args, name, None)), None)
    name = command + ("." + action if action else "")
    print(f"Collecting {name} result.", file=sys.stderr)
    capture = _Capture()
    try:
        with redirect_stdout(capture), redirect_stderr(capture):
            data = _collect(args)
        warnings, errors = _outcome(command, data)
        sanitized = redact_secrets(data)
        if data is not None and not isinstance(sanitized, dict):
            raise ValueError("result exceeds redaction limits")
        result = envelope(name, sanitized, warnings=warnings, errors=errors)
        # The same finite JSON rule applies after secret redaction.
        output = json.dumps(result, ensure_ascii=True, allow_nan=False)
        if len(output) > MAX_CAPTURE_CHARS:
            raise ValueError("result limit")
    except KeyboardInterrupt:
        result = envelope(name, None, errors=["cancelled"], exit_code=130)
        output = json.dumps(result)
    except (Exception, SystemExit) as exc:
        # Never put arbitrary exception bodies or captured progress into the
        # public error result. The human commands retain detailed diagnostics.
        code = "invalid_arguments" if isinstance(exc, JSONUsageError) else "operation_failed"
        result = envelope(name, None, errors=[code], exit_code=2 if code == "invalid_arguments" else 1)
        output = json.dumps(result)
    print(output)
    return result["exitCode"]
