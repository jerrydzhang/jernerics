import typer

from jernerics.commands import (
    backend,
    execution,
    interactive,
    investigation,
    jobs,
    project,
    tracking,
)

app = typer.Typer(help="A modern toolkit for building and evaluating ML models.")

project.register(app)
execution.register(app)
interactive.register(app)
jobs.register(app)
backend.register(app)
tracking.register(app)
investigation.register(app)


def main():
    app()
