from __future__ import annotations

from typing import TYPE_CHECKING

from jernerics.config import BackendConfig

if TYPE_CHECKING:
    from .pueue_backend import PueueBackend
    from .slurm_backend import SlurmBackend


def make_backend(
    config: BackendConfig,
    *,
    host,
    syncer=None,
    tracking_server: str | None = None,
) -> SlurmBackend | PueueBackend:
    backend_type = config.shared.type
    if backend_type == "pueue":
        from .pueue_backend import PueueBackend

        return PueueBackend.from_config(
            config, host=host, syncer=syncer, tracking_server=tracking_server
        )
    elif backend_type == "slurm":
        from .slurm_backend import SlurmBackend

        return SlurmBackend.from_config(
            config, host=host, syncer=syncer, tracking_server=tracking_server
        )
    raise ValueError(f"Unknown backend type: {backend_type}")
