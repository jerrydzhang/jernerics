from dataclasses import dataclass, field
from abc import abstractmethod, ABC
from typing import Dict, Any, Self, List, TYPE_CHECKING
from pathlib import Path
import json

import numpy as np

ParameterType = np.ndarray | List | float | int | str | bool | None


@dataclass
class Experiment(ABC):
    name: str
    description: str
    timestamp: str
    task_id: str
    results_dir: Path
    parameters: Dict[str, ParameterType]

    def save_results(self: Self, metrics: Dict[str, float]) -> None:
        results_path = (
            self.results_dir
            / f"{self.name}_{self.timestamp}"
            / f"{self.task_id}_results.json"
        )
        results_path.parent.mkdir(parents=True, exist_ok=True)

        with open(results_path, "w") as f:
            json.dump({"metrics": metrics, "parameters": self.parameters}, f, indent=4)

    def run(self: Self) -> None:
        data = self.setup_data(self.parameters)

        trained_model = self.train(data, self.parameters)

        metrics = self.evaluate(trained_model, data, self.parameters)
        self.save_results(metrics)

        result_path = self.results_dir / f"{self.name}_{self.timestamp}"
        self.save_model(result_path, trained_model)

    @abstractmethod
    def setup_data(self: Self, config: Dict[str, Any]) -> Any:
        pass

    @abstractmethod
    def save_model(self: Self, result_path: Path, model: Any) -> None:
        pass

    @abstractmethod
    def train(self: Self, data: Any, config: Dict[str, Any]) -> Any:
        pass

    @abstractmethod
    def evaluate(
        self: Self, model: Any, data: Any, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        pass
