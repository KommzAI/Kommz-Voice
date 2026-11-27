#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path


_SECRET_NAME = re.compile(r"(secret|token|password|passwd|authorization|api[-_ ]?key|salt)", re.IGNORECASE)


def sanitize_text(value: str) -> str:
    # Sel ou secret ajoute en fin de formule de signature : sha256(a + b + "SECRET")
    value = re.sub(r'(\+\s*)"[^"]{6,}"(\s*[;)])', r'\1"REDACTED_SECRET"\2', value)
    value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "admin@example.com", value)
    value = re.sub(r"https://buy\.stripe\.com/[A-Za-z0-9]+", "https://buy.stripe.com/REDACTED", value)
    value = re.sub(r"\b\d{6,}\b", "REDACTED_ID", value)
    return value


def sanitize_obj(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in {"account", "__IMTCONN__", "__IMTHOOK__"}:
                out[k] = "REDACTED_CONNECTION"
                continue
            if isinstance(v, str) and _SECRET_NAME.search(str(k)):
                out[k] = "REDACTED_SECRET"
                continue
            out[k] = sanitize_obj(v)
        # Couples {"name": "X-...-Secret", "value": "..."} (en-tetes, champs de formulaire).
        label = next((str(obj[n]) for n in ("name", "key") if isinstance(obj.get(n), str)), "")
        if label and _SECRET_NAME.search(label) and isinstance(out.get("value"), str):
            out["value"] = "REDACTED_SECRET"
        return out
    if isinstance(obj, list):
        return [sanitize_obj(v) for v in obj]
    if isinstance(obj, str):
        return sanitize_text(obj)
    return obj


def main():
    if len(sys.argv) != 3:
        print("Usage: sanitize-make-blueprint.py <input.json> <output.json>")
        sys.exit(1)

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    data = json.loads(src.read_text(encoding="utf-8"))
    sanitized = sanitize_obj(data)
    dst.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Sanitized blueprint written to: {dst}")


if __name__ == "__main__":
    main()
