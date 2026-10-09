"""Opt-in, content-free submission decisions; repository policy only restricts routes."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading

from .endpoint_policy import validate_endpoint_url


def normalize_base_url(value):
    return validate_endpoint_url(value).rstrip("/")

STAGES = {"primary", "summary", "judge", "fallback"}
REASONS = {"allowed", "acknowledged", "route_denied", "secret_detected", "scan_limit", "path_denied"}
MAX_DECISIONS = 128
HASH = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    re.compile(r"(?:sk-or-v1|sk)-[A-Za-z0-9][A-Za-z0-9._-]{12,}"),
    re.compile(r"ya29\.[A-Za-z0-9._~-]{12,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\b\s*[:=]\s*(?!YOUR_|REDACTED)[A-Za-z0-9][A-Za-z0-9._/-]{11,}"),
    re.compile(r'''(?ix)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\b["']?\s*[:=]\s*["'](?!\$\{|YOUR_|REDACTED|<)[^\s"']{8,}["']'''),
)


def digest(value: str) -> str:
    result = hashlib.sha256()
    for start in range(0, len(value), 65536):
        result.update(value[start:start + 65536].encode("utf-8"))
    return result.hexdigest()


def content_digest(prompt, media=None):
    if not media:
        return digest(prompt)
    if isinstance(media,list) and any(isinstance(item,dict) and item.get('mime')=='audio/wav' for item in media):
        from .wav_audio import valid_identity
        if not valid_identity(media):raise PolicyError('Invalid captured audio identity')
        return digest(json.dumps({'version':2,'promptSha256':digest(prompt),'audio':media},sort_keys=True,separators=(',',':')))
    if (not isinstance(media,list) or len(media)>4 or any(not isinstance(item,dict)
            or set(item)!={'index','mime','bytes','sha256'} or type(item['index']) is not int or item['index']!=index
            or not isinstance(item['mime'],str) or item['mime'] not in {'image/png','image/jpeg'} or type(item['bytes']) is not int or not 0<item['bytes']<=2*1024*1024
            or not isinstance(item['sha256'],str) or not HASH.fullmatch(item['sha256'])
            for index,item in enumerate(media,1))):
        raise PolicyError('Invalid captured media identity')
    if sum(item['bytes'] for item in media)>4*1024*1024:
        raise PolicyError('Invalid captured media identity')
    return digest(json.dumps({'version':1,'promptSha256':digest(prompt),'images':media},sort_keys=True,separators=(',',':')))


class PolicyError(ValueError):
    pass


def _invalid():
    raise PolicyError("Invalid data policy; use the documented version 1 schema")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def audit_projection(value):
    """Validate, never trust, data from stored/model-created metadata."""
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "policySha256", "decisions", "omittedDecisions"}:
        return None
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] not in {1,2} or not isinstance(value["policySha256"], str) or not HASH.fullmatch(value["policySha256"]):
        return None
    rows = value["decisions"]
    if not isinstance(rows, list) or len(rows) > MAX_DECISIONS or type(value["omittedDecisions"]) is not int or not 0 <= value["omittedDecisions"] <= 2**63 - 1:
        return None
    for row in rows:
        required = {"promptSha256", "routeSha256", "stage", "reason", "scannedChars", "secretPatternCount"}
        allowed = required | ({"unscannedMediaCount"} if value["schemaVersion"] == 2 else set())
        if not isinstance(row, dict) or not required <= set(row) <= allowed:
            return None
        if "unscannedMediaCount" in row and (type(row["unscannedMediaCount"]) is not int or not 1 <= row["unscannedMediaCount"] <= 4):
            return None
        if any(not isinstance(row[key], str) or not HASH.fullmatch(row[key]) for key in ("promptSha256", "routeSha256")):
            return None
        if not isinstance(row["stage"], str) or row["stage"] not in STAGES or not isinstance(row["reason"], str) or row["reason"] not in REASONS:
            return None
        if any(type(row[key]) is not int or not 0 <= row[key] <= 8 * 1024 * 1024 for key in ("scannedChars", "secretPatternCount")):
            return None
    return {**value, "decisions": [dict(row) for row in rows]}


class DataPolicy:
    def __init__(self, path: Path, *, root: Path, acknowledgements=()):
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > 64 * 1024:
                _invalid()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                    _invalid()
                raw = stream.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                _invalid()
            config = json.loads(raw, object_pairs_hook=_object)
            if not isinstance(config, dict) or set(config) != {"schemaVersion", "destinations", "forbiddenPaths", "maxScanChars"}:
                _invalid()
            if type(config["schemaVersion"]) is not int or config["schemaVersion"] != 1:
                _invalid()
            routes = config["destinations"]
            if not isinstance(routes, list) or not 1 <= len(routes) <= 64:
                _invalid()
            self.routes = set()
            for route in routes:
                if not isinstance(route, dict) or set(route) != {"baseUrl", "model", "stages"}:
                    _invalid()
                if not isinstance(route["baseUrl"], str) or len(route["baseUrl"]) > 2048 or not isinstance(route["model"], str) or not 1 <= len(route["model"]) <= 256:
                    _invalid()
                if any(ord(c) < 33 or ord(c) == 127 for c in route["model"]):
                    _invalid()
                stages = route["stages"]
                if not isinstance(stages, list) or not stages or any(not isinstance(stage, str) or stage not in STAGES for stage in stages):
                    _invalid()
                base = normalize_base_url(route["baseUrl"])
                self.routes.update((base, route["model"], stage) for stage in stages)
            self.patterns = config["forbiddenPaths"]
            if not isinstance(self.patterns, list) or len(self.patterns) > 256 or any(not isinstance(pattern, str) or not 1 <= len(pattern) <= 512 or pattern.startswith("/") or "\\" in pattern or ".." in pattern.split("/") or any(ord(c) < 32 for c in pattern) for pattern in self.patterns):
                _invalid()
            self.limit = config["maxScanChars"]
            if type(self.limit) is not int or not 1 <= self.limit <= 8 * 1024 * 1024:
                _invalid()
            if len(acknowledgements) > 128 or any(not isinstance(value, str) or not HASH.fullmatch(value) for value in acknowledgements):
                _invalid()
            self.acknowledgements = frozenset(acknowledgements)
            self.identity = hashlib.sha256(raw).hexdigest()
            self.root = root.resolve()
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
            _invalid()
        self.lock = threading.Lock()
        self.decisions = []
        self.omitted = 0
        self.has_media = False

    def _record(self, prompt, base_url, model, stage, reason, count=0, media=None):
        row = {"promptSha256": content_digest(prompt,media), "routeSha256": digest(base_url + "\0" + model),
               "stage": stage, "reason": reason, "scannedChars": len(prompt) if reason in {"allowed", "acknowledged", "secret_detected"} else 0, "secretPatternCount": count}
        if media:
            row['unscannedMediaCount'] = len(media)
        with self.lock:
            self.has_media = self.has_media or bool(media)
            if len(self.decisions) < MAX_DECISIONS:
                self.decisions.append(row)
            else:
                self.omitted += 1
        return row

    def check(self, *, prompt: str, base_url: str, model: str, stage: str, media=None):
        if stage not in STAGES:
            _invalid()
        base = normalize_base_url(base_url)
        if (base, model, stage) not in self.routes:
            self._record(prompt, base, model, stage, "route_denied", media=media)
            raise PolicyError("Data policy denied this destination/stage; no content was submitted to it")
        if len(prompt) > self.limit:
            self._record(prompt, base, model, stage, "scan_limit", media=media)
            raise PolicyError("Data policy scan limit exceeded; narrow scope or explicitly raise the policy limit")
        count = sum(bool(pattern.search(prompt)) for pattern in _SECRET_PATTERNS)
        acknowledged = count and content_digest(prompt,media) in self.acknowledgements
        reason = "acknowledged" if acknowledged else "secret_detected" if count else "allowed"
        row = self._record(prompt, base, model, stage, reason, count, media=media)
        if count and not acknowledged:
            raise PolicyError("Data policy detected possible credentials; remove them or explicitly acknowledge this exact assembled prompt with --acknowledge-secret-hash " + row["promptSha256"])
        return row

    def check_paths(self, paths, *, root: Path):
        for value in paths:
            path = Path(value).expanduser()
            path = path if path.is_absolute() else root / path
            try:
                logical = path.absolute().relative_to(self.root).as_posix()
                resolved = path.resolve().relative_to(self.root).as_posix()
            except (ValueError, OSError, RuntimeError):
                self._record("", "", "", "primary", "path_denied")
                raise PolicyError("Data policy refuses a source path outside its repository") from None
            for candidate in (logical, resolved):
                if ".." in candidate.split("/"):
                    raise PolicyError("Data policy refuses a non-canonical source path")
                if any(fnmatch.fnmatchcase(candidate.lower() if os.name == "nt" else candidate,
                                          pattern.lower() if os.name == "nt" else pattern) for pattern in self.patterns):
                    self._record("", "", "", "primary", "path_denied")
                    raise PolicyError("Data policy denied a selected source path; no source content was submitted")

    def audit(self):
        with self.lock:
            return {"schemaVersion": 2 if self.has_media else 1, "policySha256": self.identity, "decisions": [dict(row) for row in self.decisions], "omittedDecisions": self.omitted}
