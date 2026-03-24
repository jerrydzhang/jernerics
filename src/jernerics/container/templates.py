from importlib import resources


def get_template(template_name: str) -> str:
    templates_dir = resources.files("jernerics.templates")
    template_path = templates_dir / f"{template_name}.def"

    if not template_path.is_file():
        available = list_templates()
        raise ValueError(
            f"Template '{template_name}' not found. Available: {', '.join(available)}"
        )

    return template_path.read_text()


def list_templates() -> list[str]:
    templates_dir = resources.files("jernerics.templates")
    templates = []
    for file in templates_dir.iterdir():
        name = file.name
        if name.endswith(".def"):
            templates.append(name[:-4])
    return sorted(templates)


def generate_container_def(template_name: str = "python") -> str:
    return get_template(template_name)
