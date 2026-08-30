from jernerics_schema import Selection, SelectionTokenError, decode_selection


def decode_selection_token(token: str, *, project: str | None = None) -> Selection:
    """Parse a shared selection token in the dashboard's project context.

    ``project`` is the project the dashboard is currently showing: when
    given, a token scoped elsewhere is an error instead of a silent mix.
    """
    selection = decode_selection(token)
    if project is not None and selection.project != project:
        raise SelectionTokenError(
            f"selection token is scoped to project {selection.project!r}, "
            f"not the current project {project!r}"
        )
    return selection
