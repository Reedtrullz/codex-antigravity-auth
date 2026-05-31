# JSON Schema cleaning utility to ensure tool calling works flawlessly
# across different models routed through Antigravity.

UNSUPPORTED_KEYWORDS = [
    "$schema", "$defs", "definitions", "const", "$ref", "additionalProperties",
    "propertyNames", "title", "$id", "$comment", "minLength", "maxLength", 
    "exclusiveMinimum", "exclusiveMaximum", "pattern", "minItems", "maxItems", 
    "format", "default", "examples"
]

def clean_json_schema(schema: dict) -> dict:
    """Recursively sanitize JSON Schema for Antigravity compatibility.
    Removes unsupported keys, strips const, and handles unions (anyOf/oneOf).
    """
    if not isinstance(schema, dict):
        return schema
    
    cleaned = {}
    for k, v in schema.items():
        if k in UNSUPPORTED_KEYWORDS:
            continue
        if k == "properties" and isinstance(v, dict):
            cleaned[k] = {pk: clean_json_schema(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            cleaned[k] = clean_json_schema(v)
        elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
            # Try to flatten or pick the best option
            cleaned[k] = [clean_json_schema(opt) for opt in v if isinstance(opt, dict)]
        else:
            cleaned[k] = v
            
    # VALIDATED mode requirement: every tool parameter object schema must have at
    # least one property listed in the "required" array.
    if cleaned.get("type") == "object":
        props = cleaned.setdefault("properties", {})
        reqs = cleaned.setdefault("required", [])
        if not reqs:
            props["_placeholder"] = {
                "type": "boolean",
                "description": "Placeholder property. Always pass true."
            }
            reqs.append("_placeholder")
            
    return cleaned
