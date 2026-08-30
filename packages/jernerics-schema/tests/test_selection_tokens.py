import base64
import json
import uuid

import pytest
from jernerics_schema import (
    SELECTION_TOKEN_VERSION,
    Selection,
    SelectionTokenError,
    decode_selection,
    encode_selection,
    selection_from_json,
    selection_to_json,
)

SWEEP = uuid.UUID("aa310000-0000-4000-8000-000000000000")


def test_token_round_trip_is_byte_stable():
    selection = Selection(project="lab", sweeps=(SWEEP,), trials=(uuid.uuid4(),))
    token = encode_selection(selection)
    assert encode_selection(selection) == token
    assert decode_selection(token) == selection


def test_equivalent_id_spellings_encode_identically():
    by_uuid = Selection(project="lab", sweeps=(SWEEP,))
    by_text = Selection.model_validate({"project": "lab", "sweeps": [str(SWEEP)]})
    assert by_text == by_uuid
    assert encode_selection(by_text) == encode_selection(by_uuid)


def test_token_is_unpadded_urlsafe_base64():
    token = encode_selection(Selection(project="lab", sweeps=(SWEEP,)))
    assert "=" not in token
    assert "+" not in token and "/" not in token


def test_json_text_round_trip():
    selection = Selection(project="lab", sweeps=(SWEEP,))
    text = selection_to_json(selection)
    assert json.loads(text)["v"] == SELECTION_TOKEN_VERSION
    assert selection_from_json(text) == selection


def test_unknown_version_and_garble_are_errors():
    future = (
        base64.urlsafe_b64encode(json.dumps({"v": 99, "selection": {}}).encode())
        .decode("ascii")
        .rstrip("=")
    )
    with pytest.raises(SelectionTokenError, match="version"):
        decode_selection(future)
    with pytest.raises(SelectionTokenError):
        decode_selection("definitely-not-a-token-!!!")
    with pytest.raises(SelectionTokenError, match="malformed"):
        selection_from_json("{not json")
