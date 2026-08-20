"""Contract tests for selection, page, token, and query models."""

import uuid

import pytest
from jernerics_schema import (
    Page,
    PageToken,
    Query,
    Selection,
    decode_page_token,
    encode_page_token,
)
from pydantic import ValidationError


def test_selection_full_filters_roundtrip() -> None:
    retry_root = uuid.uuid4()
    selection = Selection(
        project="proj",
        sweeps=(uuid.uuid4(), uuid.uuid4()),
        trials=(retry_root, uuid.uuid4()),
        retry_roots=(retry_root,),
        executions=(uuid.uuid4(),),
    )
    assert Selection.model_validate_json(selection.model_dump_json()) == selection


def test_selection_filters_default_to_none() -> None:
    selection = Selection(project="proj")
    assert selection.sweeps is None
    assert selection.trials is None
    assert selection.retry_roots is None
    assert selection.executions is None


def test_page_defaults() -> None:
    assert Page().limit == 100
    assert Page().offset == 0


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 1001}, {"offset": -1}])
def test_page_bounds(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        Page(**kwargs)


def test_query_defaults() -> None:
    query = Query(selection=Selection(project="proj"))
    assert query.page == Page()


def test_page_token_roundtrip_preserves_cursor_types() -> None:
    token = PageToken(
        cursor=("00000000-0000-0000-0000-000000000000", "loss", 7),
        limit=100,
        filters={"selection": {"project": "proj"}, "json_only": False},
    )
    decoded = decode_page_token(encode_page_token(token))
    assert decoded == token
    assert decoded.cursor[2] == 7
    assert decoded.cursor[0] == "00000000-0000-0000-0000-000000000000"


def test_decode_page_token_rejects_garbage() -> None:
    for garbage in ("!!!", "e30", "bm90LWEtdG9rZW4", ""):
        with pytest.raises(ValueError):
            decode_page_token(garbage)


def test_page_token_is_frozen() -> None:
    token = PageToken(cursor=("a",), limit=10)
    with pytest.raises(ValidationError):
        token.limit = 11
