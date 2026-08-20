"""Shared presentational pieces: loading, error, and empty surfaces."""

from collections.abc import Sequence

from dash import dcc, html


def Loading(*children: html.Base | str) -> dcc.Loading:
    """Wrap content in the standard spinner surface."""
    return dcc.Loading(children=list(children), parent_style={"minHeight": "12rem"})


def Error(message: str) -> html.Div:
    """Something failed while building this view."""
    return html.Div(
        [html.H3("Something went wrong"), html.P(message)],
        className="surface surface-error",
    )


def Empty(message: str) -> html.Div:
    """Nothing to show yet — distinct from failure."""
    return html.Div(
        [html.H3("Nothing here yet"), html.P(message)],
        className="surface surface-empty",
    )


def UnderConstruction(label: str, lines: Sequence[str] = ()) -> html.Div:
    """Placeholder surface for views landing in h5d.12/.13/.14."""
    return html.Div(
        [
            html.P(label, className="construction-label"),
            html.P(
                "view under construction (h5d.12/13/14)",
                className="construction-note",
            ),
            *(html.P(line) for line in lines),
        ],
        className="surface surface-construction",
    )
