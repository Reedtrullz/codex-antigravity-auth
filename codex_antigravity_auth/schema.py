# JSON Schema cleaning utility to ensure tool calling works flawlessly
# across different models routed through Antigravity.

UNSUPPORTED_KEYWORDS = [
    "$schema", "$defs", "definitions", "const", "$ref", "additionalProperties",
    "propertyNames", "title", "$id", "$comment", "minLength", "maxLength", 
    "exclusiveMinimum", "exclusiveMaximum", "pattern", "minItems", "maxItems", 
    "format", "default", "examples"
]

def _resolve_local_ref(ref: str, root: dict) -> dict | None:
    if not ref.startswith("#/"):
        return None
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
) -> dict:
    """Recursively sanitize JSON Schema for Antigravity compatibility.
    Removes unsupported keys, strips const, and handles unions (anyOf/oneOf).
    """
    if not isinstance(schema, dict):
        return schema

    root = _root if _root is not None else schema
    seen_refs = _seen_refs or set()
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen_refs:
            schema = {k: v for k, v in schema.items() if k != "$ref"}
        else:
            resolved = _resolve_local_ref(ref, root)
            if resolved is not None:
                merged = {**resolved, **{k: v for k, v in schema.items() if k != "$ref"}}
                return clean_json_schema(merged, root, _is_root, seen_refs | {ref})
    
    cleaned = {}
    for k, v in schema.items():
        if k in UNSUPPORTED_KEYWORDS:
            continue
        if k == "required":
            raw_required = [v] if isinstance(v, str) else v
            if isinstance(raw_required, list):
                required = []
                for item in raw_required:
                    if isinstance(item, str) and item and item not in required:
                        required.append(item)
                if required:
                    cleaned[k] = required
            continue
        if k == "properties" and isinstance(v, dict):
            cleaned[k] = {pk: clean_json_schema(pv, root, False, seen_refs) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            cleaned[k] = clean_json_schema(v, root, False, seen_refs)
        elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
            # Try to flatten or pick the best option
            cleaned[k] = [clean_json_schema(opt, root, False, seen_refs) for opt in v if isinstance(opt, dict)]
        else:
            cleaned[k] = v
            
    # VALIDATED mode requires a non-empty root required list for object params.
    if _is_root and cleaned.get("type") == "object":
        props = cleaned.setdefault("properties", {})
        reqs = cleaned.setdefault("required", [])
        if not reqs:
            props["_placeholder"] = {
                "type": "boolean",
                "description": "Placeholder property. Always pass true."
            }
            reqs.append("_placeholder")
            
    return cleaned
