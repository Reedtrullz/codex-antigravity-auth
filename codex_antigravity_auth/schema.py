# JSON Schema cleaning utility to ensure tool calling works flawlessly
# across different models routed through Antigravity.

from .resource_limits import ResourceLimitError, current_limits


def _charge_schema(value, budget):
    pending = [value]
    while pending:
        item = pending.pop()
        budget[0] -= 1
        if budget[0] < 0:
            raise ResourceLimitError("schema_expansion_limit")
        if isinstance(item, str):
            # Conservative JSON string cost without allocating an escaped copy.
            budget[1] -= 2
            for character in item:
                point = ord(character)
                budget[1] -= (12 if point > 0xFFFF else 6 if point > 0x7F or point < 0x20
                              else 2 if character in {'"', "\\"} else 1)
                if budget[1] < 0:
                    raise ResourceLimitError("schema_expansion_limit")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


UNSUPPORTED_KEYWORDS = [
    "$schema", "$defs", "definitions", "const", "$ref", "additionalProperties",
    "propertyNames", "title", "$id", "$comment", "minLength", "maxLength", 
    "exclusiveMinimum", "exclusiveMaximum", "pattern", "minItems", "maxItems", 
    "format", "default", "examples"
]

def _resolve_local_ref(ref: str, root: dict) -> dict | None:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    if ref.count("/") > current_limits().schema_depth:
        raise ResourceLimitError("schema_expansion_limit")
    current = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, dict) else None

def clean_json_schema(
    schema: dict,
    _root: dict | None = None,
    _is_root: bool = True,
    _seen_refs: set[str] | None = None,
    _budget: list[int] | None = None,
    _depth: int = 0,
) -> dict:
    """Recursively sanitize JSON Schema for Antigravity compatibility.
    Removes unsupported keys, strips const, and handles unions (anyOf/oneOf).
    """
    limits = current_limits()
    budget = _budget if _budget is not None else [limits.json_nodes, limits.body_bytes]
    budget[0] -= 1
    if budget[0] < 0 or _depth > limits.schema_depth:
        raise ResourceLimitError("schema_expansion_limit")
    if not isinstance(schema, dict):
        return {}

    root = _root if _root is not None else schema
    seen_refs = _seen_refs or set()
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str):
            ref = None
        if ref is not None and ref in seen_refs:
            schema = {k: v for k, v in schema.items() if k != "$ref"}
        elif ref is not None:
            resolved = _resolve_local_ref(ref, root)
            if resolved is not None:
                merged = {**resolved, **{k: v for k, v in schema.items() if k != "$ref"}}
                return clean_json_schema(merged, root, _is_root, seen_refs | {ref}, budget, _depth + 1)
    
    cleaned = {}
    for k, v in schema.items():
        if k in UNSUPPORTED_KEYWORDS:
            continue
        _charge_schema(k, budget)
        if k == "required":
            raw_required = [v] if isinstance(v, str) else v
            if isinstance(raw_required, list):
                required = []
                seen_required = set()
                for item in raw_required:
                    if isinstance(item, str) and item and item not in seen_required:
                        required.append(item)
                        seen_required.add(item)
                if required:
                    _charge_schema(required, budget)
                    cleaned[k] = required
            continue
        if k == "properties":
            if isinstance(v, dict):
                properties = {}
                for pk, pv in v.items():
                    _charge_schema(pk, budget)
                    properties[pk] = clean_json_schema(pv, root, False, seen_refs, budget, _depth + 1)
                cleaned[k] = properties
        elif k == "items":
            if isinstance(v, dict):
                cleaned[k] = clean_json_schema(v, root, False, seen_refs, budget, _depth + 1)
        elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
            # Try to flatten or pick the best option
            cleaned[k] = [clean_json_schema(opt, root, False, seen_refs, budget, _depth + 1) for opt in v if isinstance(opt, dict)]
        else:
            _charge_schema(v, budget)
            cleaned[k] = v
            
    # VALIDATED mode requires a non-empty root required list for object params.
    if _is_root and cleaned.get("type") == "object":
        props = cleaned.setdefault("properties", {})
        reqs = cleaned.setdefault("required", [])
        if not reqs:
            if "_placeholder" in props:
                raise ValueError("Internal placeholder injection conflicts with a declared _placeholder property")
            placeholder = {"type": "boolean", "description": "Placeholder property. Always pass true."}
            _charge_schema({"_placeholder": placeholder}, budget)
            _charge_schema("_placeholder", budget)
            props["_placeholder"] = placeholder
            reqs.append("_placeholder")
            
    return cleaned
