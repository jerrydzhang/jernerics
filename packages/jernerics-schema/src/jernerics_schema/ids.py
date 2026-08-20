"""UUID identity aliases for the tracking domain.

Each alias is a plain ``uuid.UUID`` at runtime; the distinct names exist so
model fields and function signatures can state *which* identity they carry.
"""

import uuid
from typing import Annotated

from pydantic import Field

EventId = Annotated[uuid.UUID, Field(description="Identity of a tracking event")]
SweepId = Annotated[uuid.UUID, Field(description="Identity of a sweep")]
SubmissionId = Annotated[
    uuid.UUID, Field(description="Identity of a backend submission")
]
JobId = Annotated[uuid.UUID, Field(description="Identity of a scheduler job")]
TrialId = Annotated[uuid.UUID, Field(description="Identity of a trial")]
ExecutionId = Annotated[uuid.UUID, Field(description="Identity of a trial execution")]
ArtifactId = Annotated[uuid.UUID, Field(description="Identity of a declared artifact")]

JERNERICS_NAMESPACE = uuid.UUID("8f3a9c21-54e7-4b6a-8d2f-0c71e5a93b47")
"""Fixed namespace for deterministic v3 identities.

NEVER change this constant: every ``sweep_id_for`` derivation depends on
it, and a different namespace would silently split existing sweeps into
two identities on the server.
"""


def sweep_id_for(project: str, sweep_name: str) -> SweepId:
    """Deterministic sweep identity shared by deploy, runner, and post-hook."""
    return uuid.uuid5(JERNERICS_NAMESPACE, f"{project}:{sweep_name}")
