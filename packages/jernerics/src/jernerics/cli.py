import typer

from jernerics.commands import backend, execution, interactive, jobs, project, tracking

app = typer.Typer(help="A modern toolkit for building and evaluating ML models.")

execution.register(app)
interactive.register(app)
backend.register(app)
jobs.register(app)
backend.register_clean(app)
tracking.register(app)
project.register(app)


def main():
    app()
