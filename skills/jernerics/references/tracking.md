# Tracking

## Tracking server

A gRPC server receives trial events (params, metrics, artifacts) and
stores them in DuckDB. The server address is set via:

```bash
export JERNERICS_TRACKING_SERVER="host:port"
```

Or in `pyproject.toml`:

```toml
[tool.jernerics]
tracking_server = "host:port"
```

When configured, each trial streams events to the server via gRPC.
If the server is unreachable, events are still written locally and can
be synced later.

## Local tracking data

Trial events are written as protobuf files to the local tracking
directory:

```
~/.cache/jernerics/<project>/tracking/<study>/events/*.pb
```

## Replay and sync

The `sync` command replays local tracking data from a remote host to
the tracking server:

```bash
jernerics sync -b <name>
jernerics sync -b <name> --study <study_name>
```

The post-hook pipeline also runs replay automatically after a sweep
completes.

## Artifact storage

Artifacts logged via `tracker.log_artifact()` are uploaded to
S3-compatible storage (MinIO). Requires these environment variables:

```bash
export AWS_ENDPOINT_URL="https://..."
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export JERNERICS_ARTIFACT_BUCKET="jernerics"
```

The `jernerics run` command passes these env vars through to the
container (as `-e` flags for Docker, `--env` for Apptainer).

Artifact manifests track byte offsets so only new artifacts are
uploaded on each sync.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `JERNERICS_TRACKING_SERVER` | gRPC server address |
| `AWS_ENDPOINT_URL` | S3 endpoint for artifact storage |
| `AWS_ACCESS_KEY_ID` | S3 credentials |
| `AWS_SECRET_ACCESS_KEY` | S3 credentials |
| `JERNERICS_ARTIFACT_BUCKET` | S3 bucket name |
