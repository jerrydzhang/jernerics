"""Optional pandas/optuna conveniences over the typed tracking records.

These helpers are deliberately outside the core client: importing
``jernerics.tracking.client`` never pulls pandas or optuna. Users who
want a dataframe or an optuna study import this module; pandas must be
provided by the caller's environment, optuna ships with jernerics.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from jernerics_schema import Selection, TrialState
from pydantic import BaseModel

from .client import TrackingClient

if TYPE_CHECKING:
    import optuna
    import pandas  # ty: ignore[unresolved-import]

_OPTUNA_STATES: dict[TrialState, str] = {
    TrialState.WAITING: "WAITING",
    TrialState.RUNNING: "RUNNING",
    TrialState.COMPLETED: "COMPLETE",
    TrialState.FAILED: "FAIL",
    TrialState.PRUNED: "PRUNED",
}


def to_dataframe(records: Iterable[BaseModel]) -> "pandas.DataFrame":
    """Build a dataframe from frozen schema records.

    pandas is imported lazily; without it the records stay plain objects
    and this raises a clear ImportError instead.
    """
    try:
        import pandas  # ty: ignore[unresolved-import]
    except ImportError as e:
        raise ImportError("pandas is not installed; records are plain objects") from e
    rows = [record.model_dump(mode="json") for record in records]
    return pandas.DataFrame(rows)


def reconstruct_study(selection: Selection, client: TrackingClient) -> "optuna.Study":
    """Rebuild an in-memory optuna study from generic trial snapshots.

    Params come from the trial-params endpoint (sampled and manual), one
    distribution per param from the trial's recorded optuna distribution
    JSON, state maps onto the optuna trial states, and the objective from
    the trial snapshot. Params without a recorded distribution cannot be
    represented in optuna and are skipped.
    """
    import optuna
    from optuna.distributions import json_to_distribution
    from optuna.trial import TrialState as OptunaState
    from optuna.trial import create_trial

    handle = client.project(selection.project)
    trials = handle.trials(selection)
    params_by_trial: dict[uuid.UUID, dict[str, Any]] = defaultdict(dict)
    for row in handle.params(selection):
        params_by_trial[row.trial_id][row.key] = row.value

    study = optuna.create_study()
    for trial in trials:
        recorded = trial.distributions.root if trial.distributions else {}
        distributions = {
            name: json_to_distribution(str(payload))
            for name, payload in recorded.items()
        }
        candidate = params_by_trial.get(trial.trial_id, {})
        attached = {
            key: value for key, value in candidate.items() if key in distributions
        }
        study.add_trial(
            create_trial(
                state=OptunaState[_OPTUNA_STATES[trial.state]],
                params=attached,
                distributions={key: distributions[key] for key in attached},
                value=trial.objective,
            )
        )
    return study
