import base64
import binascii
import json

from .selection import Selection

SELECTION_TOKEN_VERSION = 1
"""Wire version of encoded selections; bump on breaking payload changes."""


class SelectionTokenError(Exception):
    """A selection token or its JSON form is malformed or wrongly versioned."""


def _selection_payload(selection: Selection) -> dict[str, object]:
    return {
        "v": SELECTION_TOKEN_VERSION,
        "selection": selection.model_dump(mode="json"),
    }


def selection_to_json(selection: Selection) -> str:
    """Stable versioned JSON text for a selection (sorted keys, compact)."""
    return json.dumps(
        _selection_payload(selection), sort_keys=True, separators=(",", ":")
    )


def selection_from_json(text: str) -> Selection:
    """Parse the versioned JSON form produced by :func:`selection_to_json`."""
    try:
        payload = json.loads(text)
    except ValueError as e:
        raise SelectionTokenError(f"selection JSON is malformed: {e}") from e
    if not isinstance(payload, dict) or payload.get("v") != SELECTION_TOKEN_VERSION:
        raise SelectionTokenError(
            f"unsupported selection payload: expected version {SELECTION_TOKEN_VERSION}"
        )
    try:
        return Selection.model_validate(payload["selection"])
    except (KeyError, ValueError) as e:
        raise SelectionTokenError(f"selection payload is invalid: {e}") from e


def encode_selection(selection: Selection) -> str:
    """URL-safe base64 token for a selection; byte-stable per selection.

    Round-trips through :func:`decode_selection`; powers "continue in
    Python" URL handoff from dashboards.
    """
    encoded = selection_to_json(selection).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_selection(token: str) -> Selection:
    """Decode a token from :func:`encode_selection`."""
    padded = token + "=" * (-len(token) % 4)
    try:
        text = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as e:
        raise SelectionTokenError(f"selection token is malformed: {e}") from e
    return selection_from_json(text)
