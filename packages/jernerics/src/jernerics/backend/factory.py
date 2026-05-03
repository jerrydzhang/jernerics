from jernerics.backend.backend import Backend
from jernerics.backend.submission import assemble_infrastructure


def make_backend(
    config,
    *,
    host,
    syncer=None,
    tracking_server: str | None = None,
    project_name: str = "",
) -> Backend:
    infra = assemble_infrastructure(config, host=host, project_name=project_name)

    shared = config.shared
    return Backend(
        host=host,
        infra=infra,
        syncer=syncer,
        project_name=project_name,
        tracking_server=tracking_server,
        heartbeat_interval_s=shared.heartbeat_interval_s,
        stale_after_s=shared.stale_after_s,
        grace_period_s=shared.grace_period_s,
        max_retries=shared.max_retries,
        chain_depth_cap=shared.chain_depth_cap,
    )
