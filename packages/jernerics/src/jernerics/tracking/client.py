"""Typed read client for the jernerics tracking server.

Dashboards, notebooks, scripts, and agents read tracked data through
:class:`TrackingClient` and :class:`ProjectHandle`: typed frozen records
over the domain read endpoints, opaque keyset pagination followed
transparently, and no SQL and no dataframe dependency. Expert users can
still drop to :meth:`TrackingClient.raw_query` explicitly — it is the only
place SQL is accepted.

Optional pandas/optuna conveniences live in
:mod:`jernerics.tracking.integrations`, imported only by users who want
them; this module never imports either.
"""

import base64
import binascii
import json
import os
import time
import uuid as uuid_lib
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

import httpx
from jernerics_schema import (
    ArtifactRecord,
    ArtifactSource,
    ArtifactsQuery,
    ExecutionId,
    ExecutionRecord,
    ExecutionsQuery,
    LineageQuery,
    Page,
    ProvenanceQuery,
    ProvenanceRecord,
    Selection,
    SweepId,
    SweepRecord,
    SweepsQuery,
    TrialId,
    TrialLineageRecord,
    TrialParamRecord,
    TrialParamsQuery,
    TrialRecord,
    TrialsQuery,
    ValueCatalogQuery,
    ValueCatalogRecord,
    ValueRecord,
    ValuesQuery,
)
from pydantic import TypeAdapter

from .infra import TrackingServerSchemeError, resolve_tracking_ship

SELECTION_TOKEN_VERSION = 1
"""Wire version of encoded selections; bump on breaking payload changes."""

MAX_PAGES = 10_000
"""Upper bound on pages followed per list call before raising."""

HTTP_ATTEMPTS = 3
"""Total attempts (initial plus two retries) for transient HTTP failures."""

RETRY_BACKOFF_S = 0.2
"""Linear backoff base between retries of transient HTTP failures."""

MAX_PAGE_SIZE = 1000
"""Largest page the domain read endpoints accept."""


class TrackingClientError(Exception):
    """Any tracking-client failure: configuration, transport, HTTP, auth."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _selection_payload(selection: Selection) -> dict[str, Any]:
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
        raise TrackingClientError(f"selection JSON is malformed: {e}") from e
    if not isinstance(payload, dict) or payload.get("v") != (SELECTION_TOKEN_VERSION):
        raise TrackingClientError(
            f"unsupported selection payload: expected version {SELECTION_TOKEN_VERSION}"
        )
    try:
        return Selection.model_validate(payload["selection"])
    except (KeyError, ValueError) as e:
        raise TrackingClientError(f"selection payload is invalid: {e}") from e


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
        raise TrackingClientError(f"selection token is malformed: {e}") from e
    return selection_from_json(text)


_SWEEP_RECORDS = TypeAdapter(list[SweepRecord])
_TRIAL_RECORDS = TypeAdapter(list[TrialRecord])
_TRIAL_LINEAGE_RECORDS = TypeAdapter(list[TrialLineageRecord])
_EXECUTION_RECORDS = TypeAdapter(list[ExecutionRecord])
_TRIAL_PARAM_RECORDS = TypeAdapter(list[TrialParamRecord])
_VALUE_CATALOG_RECORDS = TypeAdapter(list[ValueCatalogRecord])
_VALUE_RECORDS = TypeAdapter(list[ValueRecord])
_ARTIFACT_RECORDS = TypeAdapter(list[ArtifactRecord])
_PROVENANCE_RECORDS = TypeAdapter(list[ProvenanceRecord])

_NAMED_REDUCERS: dict[str, Callable[[list[float]], float]] = {
    "sum": lambda xs: float(sum(xs)),
    "min": lambda xs: float(min(xs)),
    "max": lambda xs: float(max(xs)),
    "mean": lambda xs: sum(xs) / len(xs),
}
_LAST = "last"


def _opt_tuple(values: Iterable[Any] | None) -> tuple[Any, ...] | None:
    return None if values is None else tuple(values)


def _id_tuple(ids: Iterable[uuid_lib.UUID | str]) -> tuple[uuid_lib.UUID, ...]:
    return tuple(uuid_lib.UUID(str(value)) for value in ids)


def _recency(record: ValueRecord) -> tuple[int, str]:
    """Scan-order rank used to break latest-step ties deterministically."""
    return (record.step, str(record.execution_id or ""))


def _artifact_id(artifact: ArtifactRecord | uuid_lib.UUID | str) -> str:
    if isinstance(artifact, ArtifactRecord):
        return str(artifact.artifact_id)
    try:
        return str(uuid_lib.UUID(str(artifact)))
    except ValueError as e:
        raise TrackingClientError(
            f"artifact reference {artifact!r} is not a UUID"
        ) from e


@dataclass(frozen=True)
class ProjectHandle:
    """Read surface for one project; every selection is pinned to it."""

    name: str
    _client: "TrackingClient"

    def selection(self) -> Selection:
        """A selection covering the whole project."""
        return Selection(project=self.name)

    def for_sweeps(self, *ids: SweepId | str) -> Selection:
        return Selection(project=self.name, sweeps=_id_tuple(ids))

    def for_trials(self, *ids: TrialId | str) -> Selection:
        return Selection(project=self.name, trials=_id_tuple(ids))

    def for_retry_roots(self, *ids: TrialId | str) -> Selection:
        return Selection(project=self.name, retry_roots=_id_tuple(ids))

    def for_executions(self, *ids: ExecutionId | str) -> Selection:
        return Selection(project=self.name, executions=_id_tuple(ids))

    def _scope(self, selection: Selection | None) -> Selection:
        scoped = selection if selection is not None else self.selection()
        if scoped.project != self.name:
            raise TrackingClientError(
                f"selection project {scoped.project!r} does not match "
                f"handle project {self.name!r}"
            )
        return scoped

    def sweeps(
        self,
        selection: Selection | None = None,
        *,
        states: Iterable[str] | None = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[SweepRecord]:
        scoped = self._scope(selection)
        state_tuple = _opt_tuple(states)
        return self._client._collect(
            "/sweeps",
            lambda page, token: SweepsQuery(
                selection=scoped,
                states=state_tuple,
                page=page,
                page_token=token,
            ),
            _SWEEP_RECORDS,
            page_size,
        )

    def trials(
        self,
        selection: Selection | None = None,
        *,
        states: Iterable[str] | None = None,
        retry_roots_only: bool = False,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[TrialRecord]:
        scoped = self._scope(selection)
        state_tuple = _opt_tuple(states)
        return self._client._collect(
            "/trials",
            lambda page, token: TrialsQuery(
                selection=scoped,
                states=state_tuple,
                retry_roots_only=retry_roots_only,
                page=page,
                page_token=token,
            ),
            _TRIAL_RECORDS,
            page_size,
        )

    def lineage(
        self,
        selection: Selection | None = None,
        *,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[TrialLineageRecord]:
        """``page_size`` is accepted for signature uniformity only; the
        lineage endpoint returns one unpaginated response."""
        del page_size
        data = self._client._post_json(
            "/lineage",
            LineageQuery(selection=self._scope(selection)).model_dump(mode="json"),
        )
        return _TRIAL_LINEAGE_RECORDS.validate_python(data["records"])

    def executions(
        self,
        selection: Selection | None = None,
        *,
        states: Iterable[str] | None = None,
        derive: bool = True,
        heartbeat_stale_s: float | None = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[ExecutionRecord]:
        """``page_size`` is accepted for signature uniformity only; the
        executions endpoint returns one unpaginated response."""
        del page_size
        query = ExecutionsQuery(
            selection=self._scope(selection),
            states=_opt_tuple(states),
            derive=derive,
            heartbeat_stale_s=heartbeat_stale_s,
        )
        data = self._client._post_json("/executions", query.model_dump(mode="json"))
        return _EXECUTION_RECORDS.validate_python(data["records"])

    def params(
        self,
        selection: Selection | None = None,
        *,
        kinds: Iterable[str] | None = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[TrialParamRecord]:
        scoped = self._scope(selection)
        kind_tuple = _opt_tuple(kinds)
        return self._client._collect(
            "/trial-params",
            lambda page, token: TrialParamsQuery(
                selection=scoped,
                kinds=kind_tuple,
                page=page,
                page_token=token,
            ),
            _TRIAL_PARAM_RECORDS,
            page_size,
        )

    def value_catalog(
        self,
        selection: Selection | None = None,
        *,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[ValueCatalogRecord]:
        """``page_size`` is accepted for signature uniformity only; the
        catalog endpoint returns one unpaginated response."""
        del page_size
        data = self._client._post_json(
            "/value-catalog",
            ValueCatalogQuery(selection=self._scope(selection)).model_dump(mode="json"),
        )
        return _VALUE_CATALOG_RECORDS.validate_python(data["records"])

    def values(
        self,
        selection: Selection | None = None,
        *,
        keys: Iterable[str] | None = None,
        steps: Iterable[int] | None = None,
        since_ns: int | None = None,
        json_only: bool = False,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[ValueRecord]:
        return list(
            self.iter_values(
                selection,
                keys=keys,
                steps=steps,
                since_ns=since_ns,
                json_only=json_only,
                page_size=page_size,
            )
        )

    def iter_values(
        self,
        selection: Selection | None = None,
        *,
        keys: Iterable[str] | None = None,
        steps: Iterable[int] | None = None,
        since_ns: int | None = None,
        json_only: bool = False,
        page_size: int = MAX_PAGE_SIZE,
    ) -> Iterator[ValueRecord]:
        """Yield value records lazily, following keyset pages on demand."""
        scoped = self._scope(selection)
        key_tuple = _opt_tuple(keys)
        step_tuple = _opt_tuple(steps)
        yield from self._client._iter_paged(
            "/values",
            lambda page, token: ValuesQuery(
                selection=scoped,
                keys=key_tuple,
                steps=step_tuple,
                since_ns=since_ns,
                json_only=json_only,
                page=page,
                page_token=token,
            ),
            _VALUE_RECORDS,
            page_size,
        )

    def artifacts(
        self,
        selection: Selection | None = None,
        *,
        keys: Iterable[str] | None = None,
        received: bool | None = None,
        source: ArtifactSource | None = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[ArtifactRecord]:
        scoped = self._scope(selection)
        key_tuple = _opt_tuple(keys)
        return self._client._collect(
            "/artifacts",
            lambda page, token: ArtifactsQuery(
                selection=scoped,
                keys=key_tuple,
                received=received,
                source=source,
                page=page,
                page_token=token,
            ),
            _ARTIFACT_RECORDS,
            page_size,
        )

    def provenance(
        self,
        selection: Selection | None = None,
        *,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[ProvenanceRecord]:
        """``page_size`` is accepted for signature uniformity only; the
        provenance endpoint returns one unpaginated response."""
        del page_size
        data = self._client._post_json(
            "/provenance",
            ProvenanceQuery(selection=self._scope(selection)).model_dump(mode="json"),
        )
        return _PROVENANCE_RECORDS.validate_python(data["records"])

    def latest_values(
        self,
        selection: Selection | None = None,
        *,
        page_size: int = MAX_PAGE_SIZE,
    ) -> dict[str, ValueRecord]:
        """Last step per key within the selection, folded client-side.

        Uses the value catalog to fetch only each key's latest-step rows;
        ties on step (same key and step logged by several executions)
        resolve to the highest execution id, so the fold is deterministic.
        """
        catalog = self.value_catalog(selection)
        if not catalog:
            return {}
        keys = tuple(sorted({entry.key for entry in catalog}))
        steps = tuple(sorted({entry.latest_step for entry in catalog}))
        latest: dict[str, ValueRecord] = {}
        for record in self.values(
            selection, keys=keys, steps=steps, page_size=page_size
        ):
            current = latest.get(record.key)
            if current is None or _recency(record) > _recency(current):
                latest[record.key] = record
        return latest

    def reduce(
        self,
        key: str,
        *,
        fn: Callable[[list[float]], float] | str = sum,
        where: Callable[[ValueRecord], bool] | None = None,
        selection: Selection | None = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> float:
        """Explicit client-side reduction over the numeric values of ``key``.

        ``fn`` is a callable over the collected numbers, or one of the
        named reductions ``"sum"``, ``"min"``, ``"max"``, ``"mean"``,
        ``"last"``; ``"last"`` returns the value :meth:`latest_values`
        would pick for the key. ``where`` filters ValueRecords before
        collection. Heavy aggregation over many points belongs in
        :meth:`TrackingClient.raw_query`, not here.
        """
        records = self.values(selection, keys=(key,), page_size=page_size)
        if where is not None:
            records = [record for record in records if where(record)]
        pairs = [
            (record, float(record.value))
            for record in records
            if isinstance(record.value, int | float)
            and not isinstance(record.value, bool)
        ]
        if not pairs:
            raise TrackingClientError(
                f"no numeric values for key {key!r} in the selection"
            )
        if fn == _LAST:
            return max(pairs, key=lambda pair: _recency(pair[0]))[1]
        if isinstance(fn, str):
            try:
                reducer = _NAMED_REDUCERS[fn]
            except KeyError:
                raise TrackingClientError(
                    f"unknown reduction {fn!r}; expected one of "
                    f"{[*sorted(_NAMED_REDUCERS), _LAST]} or a callable"
                ) from None
        else:
            reducer = fn
        return float(reducer([number for _, number in pairs]))


class TrackingClient:
    """Persistent typed read client for one tracking server.

    One ``httpx.Client`` under the hood; bearer auth when ``api_key`` is
    set. Use as a context manager, or call :meth:`close` when done.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        scheme = urlsplit(base_url).scheme
        if scheme not in ("http", "https"):
            raise TrackingClientError(
                f"base_url {base_url!r} is missing its http:// or https:// "
                f"scheme; add one, for example http://{base_url}"
            )
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_env(cls, *, timeout: float = 30.0) -> Self:
        """Build a client from the environment used across jernerics.

        Reads ``JERNERICS_TRACKING_SERVER`` for the base URL and
        ``JERNERICS_API_KEY`` for optional bearer auth — the same
        resolution the shipper and CLI use.
        """
        try:
            resolved = resolve_tracking_ship(
                os.environ.get("JERNERICS_TRACKING_SERVER", "")
            )
        except TrackingServerSchemeError as e:
            raise TrackingClientError(str(e)) from e
        if resolved is None:
            raise TrackingClientError(
                "JERNERICS_TRACKING_SERVER is not set; export the tracking "
                "server base URL, for example http://host:8000"
            )
        base_url, api_key = resolved
        return cls(base_url, api_key=api_key, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def project(self, name: str) -> ProjectHandle:
        """A handle whose selections are pinned to one project."""
        return ProjectHandle(name=name, _client=self)

    def projects(self) -> list[str]:
        """Names of every project the server knows about."""
        data = self._post_json("/projects", {})
        return list(data["records"])

    def raw_query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        *,
        limit: int = 10_000,
    ) -> dict[str, Any]:
        """Expert escape hatch: one read-only statement via ``/query``.

        Returns ``{"columns": [...], "rows": [...]}``. This is the only
        SQL surface in the client; everything else goes through typed
        domain endpoints.
        """
        data = self._post_json(
            "/query",
            {"sql": sql, "params": list(params) if params is not None else None},
        )
        rows = data["rows"]
        if len(rows) > limit:
            raise TrackingClientError(
                f"raw query returned {len(rows)} rows, above the requested "
                f"limit of {limit}; narrow the query"
            )
        return {"columns": data["columns"], "rows": rows}

    def download(
        self,
        artifact: ArtifactRecord | uuid_lib.UUID | str,
        dest: Path,
    ) -> Path:
        dest = Path(dest)
        with (
            self.open(artifact) as response,
            dest.open("wb") as out,
        ):
            for chunk in response.iter_bytes():
                out.write(chunk)
        return dest

    @contextmanager
    def open(
        self, artifact: ArtifactRecord | uuid_lib.UUID | str
    ) -> Iterator[httpx.Response]:
        """Yield the artifact's HTTP response as a binary stream.

        Iterate ``response.iter_bytes()`` (or ``response.read()`` for the
        whole blob) inside the ``with`` block.
        """
        artifact_id = _artifact_id(artifact)
        context = f"GET /artifact/{artifact_id}"
        response = self._request("GET", f"/artifact/{artifact_id}", stream=True)
        try:
            self._raise_for_status(response, context)
            yield response
        finally:
            response.close()

    def read_json(self, artifact: ArtifactRecord | uuid_lib.UUID | str) -> Any:
        """Fetch a JSON artifact and parse it; explicit ``.json()`` path."""
        with self.open(artifact) as response:
            try:
                return json.loads(response.read())
            except ValueError as e:
                raise TrackingClientError(
                    f"artifact {_artifact_id(artifact)} is not valid JSON: {e}"
                ) from e

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        stream: bool = False,
    ) -> httpx.Response:
        """Send one request, retrying transient failures (5xx, transport).

        The domain read endpoints are idempotent GET-like POSTs, so a
        bounded retry with linear backoff is safe.
        """
        failure = "unknown failure"
        for attempt in range(HTTP_ATTEMPTS):
            request = self._http.build_request(method, path, json=json_body)
            try:
                response = self._http.send(request, stream=stream)
            except httpx.TransportError as e:
                failure = f"transport error: {e!r}"
            else:
                if response.status_code < 500:
                    return response
                failure = f"HTTP {response.status_code}"
                response.close()
            if attempt + 1 < HTTP_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
        raise TrackingClientError(
            f"{method} {path} failed after {HTTP_ATTEMPTS} attempts ({failure})"
        )

    def _post_json(self, path: str, payload: Any) -> dict[str, Any]:
        response = self._request("POST", path, json_body=payload)
        self._raise_for_status(response, f"POST {path}")
        try:
            body = response.json()
        except ValueError as e:
            raise TrackingClientError(f"POST {path} returned a non-JSON body") from e
        if not isinstance(body, dict):
            raise TrackingClientError(f"POST {path} returned an unexpected body shape")
        return body

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        if response.status_code < 400:
            return
        code, detail = self._error_parts(response)
        status = response.status_code
        reason = httpx.codes.get_reason_phrase(status).upper()
        message = f"{context} failed: HTTP {status} {reason}"
        if code is not None:
            message += f" [{code}]"
        if detail:
            message += f": {detail}"
        if status == 401:
            message += (
                " — request not authorized; check the API key passed to "
                "TrackingClient (or JERNERICS_API_KEY)"
            )
        raise TrackingClientError(message, status_code=status, code=code)

    @staticmethod
    def _error_parts(response: httpx.Response) -> tuple[str | None, str]:
        """Extract (code, detail) from the server's error envelopes."""
        try:
            response.read()
            body: Any = response.json()
        except ValueError:
            return None, response.text[:300]
        if not isinstance(body, dict):
            return None, str(body)[:300]
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            return (
                str(code) if code is not None else None,
                str(error.get("detail", "")),
            )
        if isinstance(error, str):
            return None, error
        detail = body.get("detail")
        if isinstance(detail, str):
            return None, detail
        if isinstance(detail, list):
            return "request_invalid", json.dumps(detail)[:300]
        return None, str(body)[:300]

    def _page_limit(self, page_size: int) -> int:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise TrackingClientError(
                f"page_size must be between 1 and {MAX_PAGE_SIZE}, got {page_size}"
            )
        return page_size

    def _iter_paged(
        self,
        path: str,
        make_query: Callable[[Page, str | None], Any],
        adapter: TypeAdapter,
        page_size: int,
    ) -> Iterator[Any]:
        """Follow opaque keyset pages, yielding records lazily."""
        page = Page(limit=self._page_limit(page_size))
        token: str | None = None
        for _ in range(MAX_PAGES):
            query = make_query(page, token)
            data = self._post_json(path, query.model_dump(mode="json"))
            yield from adapter.validate_python(data["records"])
            token = data.get("next_token")
            if token is None:
                return
        raise TrackingClientError(
            f"{path}: pagination exceeded {MAX_PAGES} pages; the server "
            "kept returning a next token"
        )

    def _collect(
        self,
        path: str,
        make_query: Callable[[Page, str | None], Any],
        adapter: TypeAdapter,
        page_size: int,
    ) -> list[Any]:
        return list(self._iter_paged(path, make_query, adapter, page_size))
