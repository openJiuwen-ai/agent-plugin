"""Text/JSON safety helpers shared by every extractor."""
import json


def safe_text(value, max_chars: int | None = None) -> str:
    """Convert any value into JSON-safe text.

   Strings pass through unchanged. Dicts/lists/objects are JSON-dumped (with
   `default=str` so non-serializable objects stringify instead of raising).
   Optionally truncates to `max_chars`, appending an ellipsis when cut.
   Used everywhere a tool output or payload of unknown type must become text.
   """
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except Exception:
            text = str(value)

    if max_chars and len(text) > max_chars:
        return text[:max_chars] + "..."

    return text

def safe_json_loads(value):
    """Parse a value into a dict without ever raising.

   A dict is returned as-is. A JSON string is parsed. A non-JSON string is
   wrapped as ``{"raw_arguments": value}`` so the caller always gets a dict.
   Mainly used on tool-call `arguments`, which arrive as JSON strings.
   """
    if isinstance(value, dict):
        return value

    if not isinstance(value, str):
        return {}

    try:
        return json.loads(value)
    except Exception:
        return {"raw_arguments": value}

def get_nested_attr_or_dict_value(obj, *names, default=""):
    """Read the first matching key/attribute from a dict-like OR object-like value.

   tool-call items are sometimes dicts and sometimes objects depending
   on the code path; this checks each name as a dict key first, then as an
   attribute, and returns `default` if none match. Lets the extractors treat
   both shapes uniformly (e.g. look up "call_id" then "id").
   """
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj.get(name, default)

        if hasattr(obj, name):
            return getattr(obj, name, default)

    return default
