from importlib import resources

_STARTER_EXTENSIONS = (".def", ".Dockerfile")


def get_starter(starter_name: str, ext: str | None = None) -> str:
    if not starter_name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(
            f"Invalid starter name '{starter_name}': "
            "must contain only alphanumeric characters, hyphens, and underscores"
        )

    starters_dir = resources.files("jernerics.templates")

    extensions = [ext] if ext is not None else list(_STARTER_EXTENSIONS)

    for e in extensions:
        starter_path = starters_dir / f"{starter_name}{e}"
        if starter_path.is_file():
            return starter_path.read_text()

    available = list_starters()
    raise ValueError(
        f"Starter '{starter_name}' not found. Available: {', '.join(available)}"
    )


def list_starters() -> list[str]:
    starters_dir = resources.files("jernerics.templates")
    starters: set[str] = set()
    for file in starters_dir.iterdir():
        for ext in _STARTER_EXTENSIONS:
            if file.name.endswith(ext):
                starters.add(file.name[: -len(ext)])
                break
    return sorted(starters)


def generate_container_def(starter_name: str = "python") -> str:
    return get_starter(starter_name)
