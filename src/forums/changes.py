"""Change classification, OCR-vs-edit disambiguation, and cursor helpers."""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Optional
import uuid

from .models import utc_now_iso


class CursorStreamMismatchError(ValueError):
    """Raised when a consumer supplies a cursor from a different stream_id."""


def make_change_id() -> str:
    """Return a globally unique opaque change ID."""
    return f"chg-{uuid.uuid4().hex}"


def make_record_id(prefix: str = "rec") -> str:
    """Return a globally unique opaque record ID."""
    return f"{prefix}-{uuid.uuid4().hex}"


def classify_body_change(
    prev_body: str,
    new_body: str,
    prev_recognition: Optional[dict[str, Any]],
    new_recognition: Optional[dict[str, Any]],
    prev_remote_edited_at: Optional[str],
    new_remote_edited_at: Optional[str],
    explicit_edited_marker: bool = False,
) -> str:
    """Distinguish an OCR recognition correction from an actual remote edit.

    Returns 'recognition_corrected' or 'edited'.
    """
    if explicit_edited_marker:
        return "edited"
    if new_remote_edited_at is not None and new_remote_edited_at != prev_remote_edited_at:
        return "edited"

    prev_method = (prev_recognition or {}).get("method", "unknown")
    new_method = (new_recognition or {}).get("method", "unknown")
    prev_conf = (prev_recognition or {}).get("confidence")
    new_conf = (new_recognition or {}).get("confidence")

    if prev_method == "ocr" and new_remote_edited_at == prev_remote_edited_at:
        if new_method in {"accessibility", "semantic", "dom", "api"}:
            return "recognition_corrected"
        if new_method == "ocr":
            ratio = SequenceMatcher(None, prev_body, new_body).ratio()
            conf_improved = (
                prev_conf is not None
                and new_conf is not None
                and float(new_conf) > float(prev_conf)
            )
            if ratio >= 0.75 or (conf_improved and ratio >= 0.65):
                return "recognition_corrected"

    return "edited"


def build_change_event(
    entity_type: str,
    entity_ref: str,
    kind: str,
    before_revision: Optional[int],
    after_revision: Optional[int],
    source_method: str,
    source_url: Optional[str],
    confidence: Optional[float],
    after_entity: Optional[dict[str, Any]],
    observed_at: Optional[str] = None,
    change_id: Optional[str] = None,
) -> dict[str, Any]:
    """Construct a contract-compliant change entry for forum.changes.v1."""
    return {
        "change_id": change_id or make_change_id(),
        "entity_type": entity_type,
        "entity_ref": entity_ref,
        "kind": kind,
        "before_revision": before_revision,
        "after_revision": after_revision,
        "observed_at": observed_at or utc_now_iso(),
        "source": {
            "method": source_method,
            "source_url": source_url,
            "confidence": confidence,
        },
        "after": after_entity,
    }
