import base64
import json


def build_setup_command(
    *,
    study_name: str,
    storage_path: str,
    direction: str,
    work_prefix: str,
    cache_prefix: str,
    config_relpath: str = "",
    grid: dict[str, list] | None = None,
) -> str:
    sampler_expr = "None"
    if config_relpath:
        sampler_expr = (
            f"__import__('jernerics.config', fromlist=['load_config'])"
            f".load_config('{work_prefix}/{config_relpath}').sampler"
        )

    lines = [
        'python -c "',
        "from optuna.storages.journal import JournalFileBackend, JournalStorage; ",
        "import optuna, itertools, json; ",
        f"sampler = {sampler_expr}; ",
        "study = optuna.create_study(",
        f"study_name={study_name!r},",
        f" storage=JournalStorage(JournalFileBackend({storage_path!r})),",
        f" direction={direction!r},",
        " sampler=sampler,",
        " load_if_exists=True);",
    ]

    if grid:
        grid_b64 = base64.b64encode(json.dumps(grid).encode()).decode()
        lines.append(
            "import base64, os; "
            f"_sentinel = '{cache_prefix}/optuna/{study_name}.grid_enqueued';"
            f" grid = json.loads(base64.b64decode({grid_b64!r}));"
            f" keys = sorted(grid.keys());"
            f" [study.enqueue_trial(dict(zip(keys, combo, strict=True)))"
            f" for combo in itertools.product(*[grid[k] for k in keys])"
            " if not os.path.exists(_sentinel)];"
            f" os.makedirs('{cache_prefix}/optuna', exist_ok=True);"
            f" open(_sentinel, 'a').close();"
        )

    lines.append('"')
    return "".join(lines)


def build_trial_command(
    *,
    dag_relpath: str,
    config_relpath: str,
    study_name: str,
    storage_path: str,
    tracking_dir: str,
    work_prefix: str,
    project_name: str | None = None,
    tracking_server: str | None = None,
    heartbeat_interval_s: float = -1.0,
    multiline: bool = False,
) -> str:
    args = [
        "python",
        "-m",
        "jernerics.runner",
        f"{work_prefix}/{dag_relpath}",
        f"{work_prefix}/{config_relpath}",
        "--study-name",
        study_name,
        "--storage-url",
        storage_path,
        "--tracking-dir",
        tracking_dir,
    ]
    if project_name:
        args.extend(["--project-name", project_name])
    if tracking_server:
        args.extend(["--server-addr", tracking_server])
    if heartbeat_interval_s > 0:
        args.extend(["--heartbeat-interval", str(heartbeat_interval_s)])
    if multiline:
        return " \\\n        ".join(args)
    return " ".join(args)


def build_checker_command(
    ctx_path: str,
    chain_depth: int,
) -> str:
    args = [
        "python",
        "-m",
        "jernerics.retry_checker",
        "--context",
        ctx_path,
        "--chain-depth",
        str(chain_depth),
    ]
    return " ".join(args)
