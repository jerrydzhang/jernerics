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
