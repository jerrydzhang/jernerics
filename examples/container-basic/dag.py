from pathlib import Path

from container_basic import process_data, save_json

from jernerics.dag import task


@task
def load_data(config):
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    data = {"samples": config["samples"], "features": config["features"]}
    save_json(str(output_dir / "data.json"), data)
    return {"data_path": str(output_dir / "data.json")}


@task(depends_on=[load_data])
def process(load_data, config):
    output_dir = Path(config["output_dir"])

    result = process_data(samples=config["samples"], features=config["features"])
    save_json(str(output_dir / "processed.json"), result)
    return {"output": str(output_dir / "processed.json")}


@task(depends_on=[process])
def save(process, config):
    output_dir = Path(config["output_dir"])

    summary = {"status": "completed", "output_dir": str(output_dir)}
    save_json(str(output_dir / "summary.json"), summary)
    return summary
