import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import tomli_w
import typer
from typing_extensions import Annotated

from ._cli_helpers import (
    ConfigNotFound,
    ExitCode,
    NoConfigsFound,
    find_pyproject_dir,
    get_script_path,
    load_config,
    load_jernerics_config,
)
from .container.builder import ContainerBuilder
from .container.templates import generate_container_def, list_templates
from .hpc import FileSyncer, SlurmJobManager, SSHClient

app = typer.Typer(help="A modern toolkit for building and evaluating ML models.")

run_app = typer.Typer()
app.add_typer(run_app, name="run", help="Run DAG experiments.")

container_app = typer.Typer()
app.add_typer(container_app, name="container", help="Build and manage containers.")

DEFAULT_SLURM = {
    "output": ".jernerics/logs/%A_%a.out",
    "error": ".jernerics/logs/%A_%a.err",
    "max_parallel": 10,
}


@run_app.command("local")
def run_local(
    dag_file: Annotated[str, typer.Argument(help="Path to the DAG file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
    results_dir: Annotated[
        str, typer.Option("--results-dir", "-r", help="Directory to store results.")
    ] = "results",
    container: Annotated[
        str | None,
        typer.Option(
            "--container", "-c", help="Path to container tarball or .sif file"
        ),
    ] = None,
    gpu: Annotated[
        bool,
        typer.Option("--gpu/--no-gpu", help="Enable GPU support via --nv flag"),
    ] = True,
):
    dag_path = os.path.abspath(dag_file)
    config_path = os.path.abspath(config_file)
    results_path = os.path.abspath(results_dir)

    try:
        _, configs, _ = load_config(config_path)
    except NoConfigsFound as e:
        print(e)
        return

    num_configs = len(configs)
    any_failed = False

    if container:
        run_script = get_script_path("run_with_container.sh")
        container_path = os.path.abspath(container)

        for i in range(num_configs):
            print(f"Running config {i + 1}/{num_configs}", flush=True)
            result = subprocess.run(
                [
                    run_script,
                    container_path,
                    dag_path,
                    config_path,
                    results_path,
                    str(gpu).lower(),
                    str(i),
                ],
            )
            if result.returncode != 0:
                print(f"Config {i + 1} failed with code {result.returncode}")
                any_failed = True
    else:
        for i in range(num_configs):
            print(f"Running config {i + 1}/{num_configs}", flush=True)
            env = os.environ.copy()
            env["JERNERICS_CONFIG_INDEX"] = str(i)

            result = subprocess.run(
                [
                    "python",
                    "-c",
                    _get_runner_code(dag_path, config_path, i, None),
                ],
                cwd=os.path.dirname(dag_path) or ".",
                env=env,
            )

            if result.returncode != 0:
                print(f"Config {i + 1} failed with code {result.returncode}")
                any_failed = True

    if any_failed:
        sys.exit(ExitCode.GENERAL_ERROR)


@run_app.command("slurm")
def run_slurm(
    dag_file: Annotated[str, typer.Argument(help="Path to the DAG file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
    results_dir: Annotated[
        str, typer.Option("--results-dir", "-r", help="Directory to store results.")
    ] = "results",
    set_opt: Annotated[
        list[str], typer.Option("--set", "-S", help="Set SLURM option (key=value)")
    ] = [],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without submitting"),
    ] = False,
):
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    dag_path = Path(dag_file).resolve()
    config_path = Path(config_file).resolve()

    try:
        hpc_config, _ = load_jernerics_config(project_dir)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        print("Run 'jernerics init' to add [tool.jernerics] config.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    if not hpc_config.host:
        print(
            "Error: No HPC host configured.\n"
            "  Set JERNERICS_HPC_HOST environment variable, or\n"
            "  Add host to [tool.jernerics.hpc] in pyproject.toml"
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        config_slurm, configs, _ = load_config(str(config_path))
    except NoConfigsFound as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    num_configs = len(configs)

    cli_overrides = {}
    for opt in set_opt:
        if "=" not in opt:
            print(f"Error: Invalid --set option: {opt}. Expected format: key=value")
            raise SystemExit(ExitCode.CONFIG_ERROR)
        key, value = opt.split("=", 1)
        cli_overrides[key] = value

    project_name = project_dir.name
    remote_dir = hpc_config.remote_dir.replace("{project_name}", project_name)
    remote_dir = remote_dir.replace("{project-name}", project_name)

    slurm_opts = {
        **DEFAULT_SLURM,
        "partition": hpc_config.partition,
        "time": hpc_config.time,
        "mem": hpc_config.mem,
        **config_slurm,
        **cli_overrides,
    }

    max_parallel = slurm_opts.pop("max_parallel", hpc_config.max_concurrent_jobs)
    max_parallel_val = int(max_parallel) if max_parallel else 0

    if max_parallel_val > 0:
        array_spec = f"1-{num_configs}%{max_parallel_val}"
    else:
        array_spec = f"1-{num_configs}"

    dag_basename = dag_path.name
    config_basename = config_path.name

    script_lines = [
        "#!/usr/bin/env bash",
        "#SBATCH --parsable",
        f"#SBATCH --array={array_spec}",
    ]
    for key, value in slurm_opts.items():
        script_lines.append(f"#SBATCH --{key}={value}")
    script_lines.append("")
    script_lines.append("CONFIG_INDEX=$((SLURM_ARRAY_TASK_ID - 1))")
    script_lines.append(f"export JERNERICS_DAG_FILE=/work/{dag_basename}")
    script_lines.append(f"export JERNERICS_CONFIG_FILE=/work/{config_basename}")
    script_lines.append("export JERNERICS_CONFIG_INDEX=$CONFIG_INDEX")
    script_lines.append(f"cd {remote_dir}")
    script_lines.append(
        f'apptainer exec --contain --nv --pwd /work --bind "{remote_dir}:/work" container.sif \\'
    )
    script_lines.append("    python -c \"$(cat <<'EOF'")
    script_lines.append("import os")
    script_lines.append("import sys")
    script_lines.append("import pathlib")
    script_lines.append("")
    script_lines.append('dag_file = os.environ["JERNERICS_DAG_FILE"]')
    script_lines.append('config_file = os.environ["JERNERICS_CONFIG_FILE"]')
    script_lines.append('config_index = int(os.environ["JERNERICS_CONFIG_INDEX"])')
    script_lines.append("")
    script_lines.append("sys.path.insert(0, str(pathlib.Path(dag_file).parent))")
    script_lines.append("")
    script_lines.append("from jernerics.dag import DAG")
    script_lines.append("from jernerics._cli_helpers import load_config")
    script_lines.append("")
    script_lines.append("dag = DAG(dag_file)")
    script_lines.append("slurm_opts, configs, max_workers = load_config(config_file)")
    script_lines.append("config = configs[config_index]")
    script_lines.append("")
    script_lines.append(
        "results = dag.run(config, config_index=config_index, config_path=config_file, max_workers=max_workers)"
    )
    script_lines.append("")
    script_lines.append(
        "failed = [name for name, result in results.items() if isinstance(result, Exception)]"
    )
    script_lines.append("if failed:")
    script_lines.append(
        '    print("DAG failed. Tasks with errors:", ", ".join(failed))'
    )
    script_lines.append("    sys.exit(1)")
    script_lines.append("else:")
    script_lines.append('    print("DAG completed")')
    script_lines.append("EOF")
    script_lines.append(')"')

    script_content = "\n".join(script_lines)

    if dry_run:
        print("=== DRY RUN ===")
        print(f"Host: {hpc_config.host}")
        print(f"Remote dir: {remote_dir}")
        print(f"Project dir: {project_dir}")
        print()
        print("=== SLURM SCRIPT ===")
        print(script_content)
        return

    ssh = SSHClient(hpc_config.host)
    syncer = FileSyncer(ssh, remote_dir)
    slurm = SlurmJobManager(ssh)

    print(f"[1/3] Syncing project to {hpc_config.host}:{remote_dir}...")
    syncer.sync_project(project_dir)

    print("[2/3] Ensuring log directory exists...")
    ssh.run(f"mkdir -p {remote_dir}/.jernerics/logs")

    if not syncer.container_exists():
        print(
            "Error: container.sif not found on remote.\n"
            "  Run 'jernerics container build' first."
        )
        raise SystemExit(ExitCode.CONTAINER_ERROR)

    print("[3/3] Submitting job...")
    try:
        job_id = slurm.submit_inline(script_content)
        print(f"\nJob submitted: {job_id}")
        print("\nMonitor progress:")
        print(f"  jernerics logs {job_id} --follow")
    except RuntimeError as e:
        print(f"Error: Failed to submit job: {e}")
        raise SystemExit(ExitCode.SLURM_ERROR)


def _get_runner_code(
    dag_file: str,
    config_file: str,
    config_index: int,
    container_path: str | None = None,
) -> str:
    container_arg = repr(container_path) if container_path else "None"
    # ruff: noqa: E501
    return f'''
import os
import sys
import pathlib

dag_file = os.environ.get("JERNERICS_DAG_FILE", {dag_file!r})
config_file = os.environ.get("JERNERICS_CONFIG_FILE", {config_file!r})
config_index = int(os.environ.get("JERNERICS_CONFIG_INDEX", "{config_index}"))
container_path = {container_arg}

dag_dir = pathlib.Path(dag_file).parent
if str(dag_dir) not in sys.path:
    sys.path.insert(0, str(dag_dir))

from jernerics.dag import DAG
from jernerics._cli_helpers import load_config

dag = DAG(dag_file)
slurm_opts, configs, max_workers = load_config(config_file)
config = configs[config_index]

results = dag.run(config, config_index=config_index, config_path=config_file, container_path=container_path, max_workers=max_workers)

failed = [name for name, result in results.items() if isinstance(result, Exception)]
if failed:
    print("DAG failed. Tasks with errors:", ", ".join(failed))
    sys.exit(1)
else:
    print("DAG completed")
'''


def _get_hpc_client():
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        hpc_config, _ = load_jernerics_config(project_dir)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        print("Run 'jernerics init' to add [tool.jernerics] config.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    if not hpc_config.host:
        print(
            "Error: No HPC host configured.\n"
            "  Set JERNERICS_HPC_HOST environment variable, or\n"
            "  Add host to [tool.jernerics.hpc] in pyproject.toml"
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)

    project_name = project_dir.name
    remote_dir = hpc_config.remote_dir.replace("{project_name}", project_name)
    remote_dir = remote_dir.replace("{project-name}", project_name)

    ssh = SSHClient(hpc_config.host)
    syncer = FileSyncer(ssh, remote_dir)
    slurm = SlurmJobManager(ssh)

    return ssh, syncer, slurm, remote_dir


@app.command("jobs")
def jobs(
    all: Annotated[
        bool,
        typer.Option("--all", "-a", help="Include completed jobs"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    _, _, slurm, _ = _get_hpc_client()

    job_list = slurm.list_jobs(include_completed=all)

    if json_output:
        data = [
            {
                "job_id": job.job_id,
                "name": job.name,
                "status": job.status,
                "partition": job.partition,
                "time": job.time,
                "nodes": job.nodes,
            }
            for job in job_list
        ]
        print(json.dumps(data, indent=2))
        return

    if not job_list:
        print("No jobs found.")
        return

    print(f"{'JOB_ID':<12} {'NAME':<20} {'STATUS':<12} {'PARTITION':<12} {'TIME':<10}")
    print("-" * 70)
    for job in job_list:
        print(
            f"{job.job_id:<12} {job.name[:20]:<20} {job.status:<12} {job.partition:<12} {job.time:<10}"
        )


@app.command("cancel")
def cancel(
    job_id: Annotated[
        str | None,
        typer.Argument(help="Job ID to cancel"),
    ] = None,
    all: Annotated[
        bool,
        typer.Option("--all", "-a", help="Cancel all your jobs"),
    ] = False,
):
    _, _, slurm, _ = _get_hpc_client()

    if all:
        if slurm.cancel_all():
            print("Cancelled all jobs.")
        else:
            print("Failed to cancel jobs.")
        return

    if job_id is None:
        print("Error: Specify a job ID or use --all")
        raise SystemExit(ExitCode.GENERAL_ERROR)

    if slurm.cancel(job_id):
        print(f"Cancelled job {job_id}.")
    else:
        print(f"Failed to cancel job {job_id}.")


@app.command("logs")
def logs(
    job_id: Annotated[str, typer.Argument(help="Job ID")],
    follow: Annotated[
        bool,
        typer.Option("--follow", "-f", help="Follow log output (tail -f)"),
    ] = False,
    array_index: Annotated[
        int | None,
        typer.Option("--array-index", "-i", help="Array task index (for array jobs)"),
    ] = None,
):
    ssh, _, _, remote_dir = _get_hpc_client()

    output_dir = f"{remote_dir}/.jernerics/logs"

    if array_index is not None:
        log_file = f"{output_dir}/{job_id}_{array_index}.out"
    else:
        log_file = f"{output_dir}/{job_id}.out"

    if follow:
        subprocess.run(["ssh", ssh.host, f"tail -f {log_file}"])
    else:
        result = ssh.run(f"cat {log_file}", check=False)
        if result.returncode != 0:
            print(f"Error: Log file not found: {log_file}")
            raise SystemExit(ExitCode.GENERAL_ERROR)
        print(result.stdout)


@app.command("results")
def results(
    job_id: Annotated[str, typer.Argument(help="Job ID")],
    local_dir: Annotated[
        str | None,
        typer.Option("--local-dir", "-d", help="Local directory to download to"),
    ] = None,
):
    _, syncer, _, remote_dir = _get_hpc_client()

    if local_dir is None:
        local_dir = f"results/{job_id}"

    remote_results = f"{remote_dir}/results"

    print(
        f"Downloading results from {syncer.ssh.host}:{remote_results} to {local_dir}..."
    )

    Path(local_dir).mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["scp", "-r", f"{syncer.ssh.host}:{remote_results}/.", local_dir],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print(f"Results downloaded to {local_dir}")
    else:
        print(f"Error: Failed to download results: {result.stderr.strip()}")
        raise SystemExit(ExitCode.SSH_ERROR)


@app.command("shell")
def shell(
    gpu: Annotated[
        int | None,
        typer.Option("--gpu", "-g", help="Number of GPUs (0 = no GPU)"),
    ] = None,
    cpus: Annotated[
        int | None,
        typer.Option("--cpus", "-c", help="Number of CPUs"),
    ] = None,
    mem: Annotated[
        str | None,
        typer.Option("--mem", "-m", help="Memory allocation (e.g., 4G)"),
    ] = None,
    time: Annotated[
        str | None,
        typer.Option("--time", "-t", help="Time limit (e.g., 1:00:00)"),
    ] = None,
    partition: Annotated[
        str | None,
        typer.Option("--partition", "-p", help="Partition name"),
    ] = None,
    no_container: Annotated[
        bool,
        typer.Option("--no-container", help="Don't enter container"),
    ] = False,
):
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        hpc_config, shell_config = load_jernerics_config(project_dir)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        print("Run 'jernerics init' to add [tool.jernerics] config.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    if not hpc_config.host:
        print(
            "Error: No HPC host configured.\n"
            "  Set JERNERICS_HPC_HOST environment variable, or\n"
            "  Add host to [tool.jernerics.hpc] in pyproject.toml"
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)

    gpu_val = gpu if gpu is not None else shell_config.gpu
    cpus_val = cpus if cpus is not None else shell_config.cpus
    mem_val = mem if mem is not None else shell_config.mem
    time_val = time if time is not None else shell_config.time
    partition_val = partition if partition is not None else shell_config.partition

    project_name = project_dir.name
    remote_dir = hpc_config.remote_dir.replace("{project_name}", project_name)
    remote_dir = remote_dir.replace("{project-name}", project_name)

    srun_args = ["srun", "--pty"]
    if partition_val:
        srun_args.extend(["--partition", partition_val])
    if cpus_val is not None:
        srun_args.extend(["--cpus-per-task", str(cpus_val)])
    if mem_val:
        srun_args.extend(["--mem", mem_val])
    if time_val:
        srun_args.extend(["--time", time_val])
    if gpu_val and gpu_val > 0:
        srun_args.extend(["--gres", f"gpu:{gpu_val}"])

    ssh = SSHClient(hpc_config.host)
    syncer = FileSyncer(ssh, remote_dir)

    if not no_container and syncer.container_exists():
        shell_cmd = (
            f"cd {remote_dir} && "
            f"{' '.join(srun_args)} "
            f"apptainer exec --nv --bind {remote_dir}:/work --pwd /work container.sif bash"
        )
    else:
        shell_cmd = f"cd {remote_dir} && {' '.join(srun_args)} bash"

    print(f"Starting interactive shell on {hpc_config.host}...")
    subprocess.run(["ssh", "-t", hpc_config.host, shell_cmd])


@app.command("clean")
def clean(
    results: Annotated[
        bool,
        typer.Option("--results", help="Delete results/ directory"),
    ] = False,
    logs: Annotated[
        bool,
        typer.Option("--logs", help="Delete .jernerics/logs/ directory"),
    ] = False,
    container: Annotated[
        bool,
        typer.Option("--container", help="Delete container.sif"),
    ] = False,
    all: Annotated[
        bool,
        typer.Option("--all", "-a", help="Delete everything"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Actually delete (dry-run by default)"),
    ] = False,
):
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        hpc_config, _ = load_jernerics_config(project_dir)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        print("Run 'jernerics init' to add [tool.jernerics] config.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    if not hpc_config.host:
        print(
            "Error: No HPC host configured.\n"
            "  Set JERNERICS_HPC_HOST environment variable, or\n"
            "  Add host to [tool.jernerics.hpc] in pyproject.toml"
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)

    project_name = project_dir.name
    remote_dir = hpc_config.remote_dir.replace("{project_name}", project_name)
    remote_dir = remote_dir.replace("{project-name}", project_name)

    ssh = SSHClient(hpc_config.host)

    to_delete = []
    if all:
        to_delete = ["results/", ".jernerics/logs/", "container.sif"]
    else:
        if results:
            to_delete.append("results/")
        if logs:
            to_delete.append(".jernerics/logs/")
        if container:
            to_delete.append("container.sif")

    if not to_delete:
        print(
            "Error: Nothing to clean. Specify --results, --logs, --container, or --all"
        )
        raise SystemExit(ExitCode.GENERAL_ERROR)

    print(f"Would delete from {hpc_config.host}:{remote_dir}:")
    for item in to_delete:
        print(f"  - {item}")

    if not force:
        print("\nDry run. Use --force to actually delete.")
        return

    for item in to_delete:
        path = f"{remote_dir}/{item}"
        result = ssh.run(f"rm -rf {path}", check=False)
        if result.returncode != 0:
            print(f"Failed to delete {item}: {result.stderr}")
        else:
            print(f"Deleted: {item}")


def main():
    app()


@app.command("init")
def init(
    project_dir: Annotated[
        str, typer.Argument(help="Directory to initialize (default: current)")
    ] = ".",
    template: Annotated[
        str, typer.Option("--template", "-t", help="Container template to use")
    ] = "python",
    force: Annotated[
        bool,
        typer.Option(
            "--force", "-f", help="Overwrite existing [tool.jernerics] config"
        ),
    ] = False,
):
    project_path = Path(project_dir).resolve()
    project_name = project_path.name

    if template not in list_templates():
        print(
            f"Error: Unknown template: {template}. Available: {', '.join(list_templates())}"
        )
        raise SystemExit(ExitCode.GENERAL_ERROR)

    project_path.mkdir(parents=True, exist_ok=True)

    pyproject_path = project_path / "pyproject.toml"
    jernerics_config = _get_default_jernerics_config(project_name)

    if pyproject_path.exists():
        with open(pyproject_path, "rb") as f:
            existing = tomllib.load(f)

        has_jernerics = "jernerics" in existing.get("tool", {})

        if has_jernerics and not force:
            if not typer.confirm(
                "[tool.jernerics] already exists in pyproject.toml. Overwrite?",
                default=False,
            ):
                print("Skipped updating pyproject.toml")
                return

        existing.setdefault("tool", {})["jernerics"] = jernerics_config
        merged = existing
    else:
        merged = _create_minimal_pyproject(project_name, jernerics_config)

    with open(pyproject_path, "wb") as f:
        tomli_w.dump(merged, f)

    print("Updated: pyproject.toml")

    container_def_path = project_path / "container.def"
    if not container_def_path.exists():
        container_def_path.write_text(generate_container_def(template))
        print("Created: container.def")
    else:
        print("Skipped: container.def (already exists)")

    src_dir = project_path / "src"
    if not src_dir.exists():
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "__init__.py").write_text('"""Project source."""\n')
        print("Created: src/")
    else:
        print("Skipped: src/ (already exists)")

    uv_result = subprocess.run(
        ["uv", "sync"],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if uv_result.returncode == 0:
        print("Generated: uv.lock")
    else:
        print(f"Warning: 'uv sync' failed: {uv_result.stderr.strip()}")

    print(f"\nProject initialized in {project_path}")
    print("\nNext steps:")
    print("  1. Edit pyproject.toml to add dependencies")
    print(
        "  2. Create your DAG and config files (e.g., experiments/dag.py, configs/default.py)"
    )
    print("  3. Run 'jernerics container build' to build on HPC")


def _get_default_jernerics_config(project_name: str) -> dict:
    return {
        "hpc": {
            "host": "your-username@hpc.example.edu",
            "remote_dir": f"~/experiments/{project_name}",
        },
        "container": {
            "partition": "priority",
            "time": "1:00:00",
            "mem": "16G",
            "cpus": 4,
        },
        "shell": {
            "partition": "priority",
            "cpus": 1,
            "mem": "4G",
            "gpu": 0,
        },
    }


def _create_minimal_pyproject(project_name: str, jernerics_config: dict) -> dict:
    return {
        "project": {
            "name": project_name,
            "version": "0.1.0",
            "description": "Add description here",
            "requires-python": ">=3.12",
            "dependencies": ["jernerics"],
        },
        "tool": {
            "uv": {
                "sources": {
                    "jernerics": {"git": "https://github.com/jerrydzhang/jernerics.git"}
                }
            },
            "jernerics": jernerics_config,
        },
        "build-system": {
            "requires": ["hatchling"],
            "build-backend": ["hatchling.build"],
        },
    }


@container_app.command("build")
def container_build(
    project_dir: Annotated[
        str, typer.Argument(help="Project directory (default: current)")
    ] = ".",
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Force rebuild even if up to date"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview actions without executing"),
    ] = False,
):
    try:
        builder = ContainerBuilder(project_dir)
        builder.build(force=force, dry_run=dry_run)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Run 'jernerics init' to create pyproject.toml")
        raise SystemExit(ExitCode.CONFIG_ERROR)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        print("Run 'jernerics init' to add [tool.jernerics] config.")
        raise SystemExit(ExitCode.CONFIG_ERROR)
    except ValueError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONTAINER_ERROR)
