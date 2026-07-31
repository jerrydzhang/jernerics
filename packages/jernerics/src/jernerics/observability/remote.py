"""HTTP adapter exposing the tracking server's ``/query`` endpoint as a
:class:`Queryable`, so CLI commands can run the same parameterised
queries tests run against an in-process store.
"""

import httpx


class RemoteStore:
    """Thin HTTP client over ``POST /query``.

    Implements :class:`Queryable`, so it can be passed to any analysis
    function. Raises :class:`RuntimeError` if the server reports a query
    error; raises :class:`httpx.HTTPStatusError` on a non-2xx status.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._url = base_url.rstrip("/") + "/query"
        self._headers = {"authorization": f"Bearer {api_key}"} if api_key else None
        self._timeout = timeout

    def query(
        self, sql: str, params: list | None = None
    ) -> tuple[list[str], list[tuple]]:
        payload: dict = {"sql": sql}
        if params is not None:
            payload["params"] = params
        response = httpx.post(
            self._url,
            json=payload,
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        columns = data["columns"]
        rows = [tuple(r) for r in data["rows"]]
        return columns, rows
