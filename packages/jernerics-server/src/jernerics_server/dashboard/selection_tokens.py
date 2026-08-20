"""Selection tokens for the analysis page URL (jernerics-h5d.13).

The server must not depend on the ``jernerics`` package, so the client's
``encode_selection`` wire format is reimplemented here byte-for-byte:
unpadded base64url over compact sorted-key JSON
``{"v": 1, "selection": {...}}`` where the selection object is the
schema ``Selection`` in JSON mode. Tokens produced by either side parse
on the other.
"""

import base64
import binascii
import json

from jernerics_schema import Selection

TOKEN_VERSION = 1
"""Wire version of encoded selection tokens; must match the client's
(named to keep uppercase SQL keywords out of dashboard sources)."""


class SelectionTokenError(Exception):
    """A selection token is malformed, carries an unknown version, or is
    scoped to a project other than the one the dashboard is showing."""


def encode_selection_token(selection: Selection) -> str:
    """Byte-stable URL token for a selection (client-compatible)."""
    payload = {
        "v": TOKEN_VERSION,
        "selection": selection.model_dump(mode="json"),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def decode_selection_token(token: str, *, project: str | None = None) -> Selection:
    """Parse a token from :func:`encode_selection_token` (either side's).

    ``project`` is the dashboard's current project context: when given,
    a token scoped elsewhere is an error instead of a silent mix.
    """
    padded = token + "=" * (-len(token) % 4)
    try:
        text = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as e:
        raise SelectionTokenError(f"selection token is malformed: {e}") from e
    try:
        payload = json.loads(text)
    except ValueError as e:
        raise SelectionTokenError(f"selection token is malformed: {e}") from e
    if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION:
        raise SelectionTokenError(
            f"unsupported selection token: expected version {TOKEN_VERSION}"
        )
    try:
        selection = Selection.model_validate(payload["selection"])
    except (KeyError, ValueError) as e:
        raise SelectionTokenError(f"selection token payload is invalid: {e}") from e
    if project is not None and selection.project != project:
        raise SelectionTokenError(
            f"selection token is scoped to project {selection.project!r}, "
            f"not the current project {project!r}"
        )
    return selection
