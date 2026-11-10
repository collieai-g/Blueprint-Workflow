"""Text-only storage boundary; this is redaction, not a credential vault."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
MEDIA_OMITTED = "[MEDIA OMITTED]"
_SECRET = re.compile(r"password|passwd|(?:^|_)pwd$|secret|(?:access|refresh|api|auth)[_-]?(?:key|token)|authorization|cookies?|credentials?|one[_-]?time[_-]?(?:password|code)|(?:^|_)(?:otp|cvv|cvc|pin)$", re.I)
_TOKEN = re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16})\b")
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_ASSIGNMENT = re.compile(r"\b(password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", re.I)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_MEDIA = re.compile(r"data:(?:image|audio|video)/[^\s]+", re.I)
_BASE64 = re.compile(r"^[A-Za-z0-9+/\r\n]{2048,}={0,2}$")
MAX_TEXT = 32000


def sensitive_key(key: str) -> bool:
    return bool(_SECRET.search(key))


def clean_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if not host:
            return "[INVALID URL]"
        authority = f"[{host}]" if ":" in host else host
        if parts.port:
            authority += f":{parts.port}"
        query = [(key, REDACTED if sensitive_key(key) or key.lower() in {"token", "key", "signature", "sig"} else item) for key, item in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, authority, parts.path, urlencode(query), ""))
    except ValueError:
        return "[INVALID URL]"


def redact_text(value: str) -> str:
    value = _MEDIA.sub(MEDIA_OMITTED, value)
    if _BASE64.fullmatch(value):
        return MEDIA_OMITTED
    value = _URL.sub(lambda match: clean_url(match.group(0)), value)
    value = _BEARER.sub("Bearer " + REDACTED, value)
    value = _TOKEN.sub(REDACTED, value)
    value = _ASSIGNMENT.sub(lambda match: match.group(1) + "=" + REDACTED, value)
    if len(value) > MAX_TEXT:
        value = value[:MAX_TEXT] + "\n[TRUNCATED: provide a semantic summary or artifact reference]"
    return value


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 32:
        raise ValueError("Record nesting exceeds 32 levels")
    if isinstance(value, dict):
        content_type = str(value.get("type", "")).lower()
        mime = str(value.get("mimeType", value.get("mime_type", ""))).lower()
        if content_type in {"image", "image_url", "input_image", "audio", "video"} or mime.startswith(("image/", "audio/", "video/")):
            return {"type": "omitted_media", "description": MEDIA_OMITTED}
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Record keys must be strings")
            if sensitive_key(key):
                result[key] = REDACTED
            elif key.lower() in {"image_url", "screenshot", "image_data", "image_base64", "audio_data", "video_data"}:
                result[key] = MEDIA_OMITTED
            else:
                result[key] = sanitize(item, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError("Records must contain only JSON values")
