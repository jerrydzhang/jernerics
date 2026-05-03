from importlib import resources


def get_starter(starter_name: str) -> str:
    if not starter_name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(
            f"Invalid starter name '{starter_name}': "
            "must contain only alphanumeric characters, hyphens, and underscores"
        )
    starters_dir = resources.files("jernerics.templates")
    starter_path = starters_dir / f"{starter_name}.def"

    if not starter_path.is_file():
        available = list_starters()
        raise ValueError(
            f"Starter '{starter_name}' not found. Available: {', '.join(available)}"
        )

    return starter_path.read_text()


def list_starters() -> list[str]:
    starters_dir = resources.files("jernerics.templates")
    starters = []
    for file in starters_dir.iterdir():
        name = file.name
        if name.endswith(".def"):
            starters.append(name[:-4])
    return sorted(starters)


def generate_container_def(starter_name: str = "python") -> str:
    return get_starter(starter_name)
