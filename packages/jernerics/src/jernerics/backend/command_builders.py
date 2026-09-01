import base64
import json
from typing import Any


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
    trial_relpath: str,
    config_relpath: str,
    study_name: str,
    storage_path: str,
    tracking_dir: str,
    work_prefix: str,
    project_name: str | None = None,
    tracking_server: str | None = None,
    heartbeat_interval_s: float = -1.0,
    param_overrides: dict[str, Any] | None = None,
    multiline: bool = False,
) -> str:
    args = [
        "python",
        "-m",
        "jernerics.runner",
        f"{work_prefix}/{trial_relpath}",
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
    if param_overrides:
        po_b64 = base64.b64encode(json.dumps(param_overrides).encode()).decode()
        args.extend(["--param-overrides", po_b64])
    if multiline:
        return " \\\n        ".join(args)
    return " ".join(args)


def build_post_hook_command(
    ctx_path: str,
    chain_depth: int,
    tracking_dir: str,
    tracking_server: str | None = None,
) -> str:
    args = [
        "python",
        "-m",
        "jernerics.post_hook",
        "--context",
        ctx_path,
        "--chain-depth",
        str(chain_depth),
        "--tracking-dir",
        tracking_dir,
    ]
    if tracking_server:
        args.extend(["--server-addr", tracking_server])
    return " ".join(args)


def build_sweep_commands(
    spec,  # SweepSubmission
    container,
    paths,  # PathResolver
    direction: str,
    tracking_server: str | None = None,
    heartbeat_interval_s: float = -1.0,
    multiline: bool = False,
    retry_ctx_path: str | None = None,
    chain_depth: int = 0,
    env_file: str | None = None,
) -> tuple[str, str, str | None]:
    cache_host = paths.resolve_cache()
    bind_args = paths.bind_args(cache_host)

    trial_relpath = spec.trial_relpath or str(spec.trial_path.name)
    config_relpath = spec.config_relpath or str(spec.config_path.name)

    setup_cmd = build_setup_command(
        study_name=spec.study_name,
        storage_path=spec.storage_url,
        direction=direction,
        config_relpath=config_relpath,
        grid=spec.grid,
        work_prefix=paths.work_prefix,
        cache_prefix=paths.cache_prefix,
    )
    wrapped_setup = container.wrap(setup_cmd, bind_args)

    tracking_dir = paths.tracking_dir(spec.study_name)
    trial_cmd = build_trial_command(
        trial_relpath=trial_relpath,
        config_relpath=config_relpath,
        study_name=spec.study_name,
        storage_path=spec.storage_url,
        project_name=spec.project_name,
        tracking_dir=tracking_dir,
        tracking_server=tracking_server,
        heartbeat_interval_s=heartbeat_interval_s,
        param_overrides=spec.param_overrides,
        work_prefix=paths.work_prefix,
        multiline=multiline,
    )
    wrapped_trial = container.wrap(trial_cmd, bind_args, env_file=env_file)

    ctx_path = retry_ctx_path or paths.retry_ctx_path(spec.study_name)
    checker_cmd = build_post_hook_command(
        ctx_path,
        chain_depth,
        tracking_dir,
        tracking_server=tracking_server,
    )
    retry_script = f"/tmp/jernerics_{spec.study_name}_retry_d{chain_depth}.sh"
    post_hook_command = container.wrap(
        f"{checker_cmd} > {retry_script} && bash {retry_script}",
        bind_args,
        env_file=env_file,
    )

    return wrapped_setup, wrapped_trial, post_hook_command
