from typing import List, Tuple, Dict, Self, Any
import numpy as np
from collections import deque
import importlib

from .data import BaseDataGenerator, BaseDataTransformer
from .labels import BaseLabelGenerator, BaseLabelComputer
from .parameters import BaseParameterGenerator, BaseParameterTransformer


def _get_class_from_string(class_path: str) -> Any:
    """Dynamically imports a class from a string path like 'module.ClassName'."""
    try:
        module_path, class_name = class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (ImportError, AttributeError, ValueError) as e:
        raise ImportError(f"Could not import class '{class_path}'. Error: {e}")


class Step:
    def __init__(
        self,
        step,
        deps: List[str] = [],
    ) -> None:
        self.step = step
        self.deps = deps


class DataGenerator:
    def __init__(self) -> None:
        self.graph: Dict[str, Step] = {}

    def add_step(self, label: str, step, deps: List[str] = []) -> Self:
        if label in self.graph:
            raise ValueError(f"Step with label '{label}' already exists.")

        for dep in deps:
            if dep not in self.graph:
                raise ValueError(f"Dependency '{dep}' for step '{label}' not found.")

        self.graph[label] = Step(step, deps)

        return self

    @classmethod
    def from_config(cls, config: Dict[str, Dict[str, Any]]) -> "DataGenerator":
        """
        Constructs a DataGenerator instance from a configuration dictionary.

        The configuration should be a dictionary where each key is a step label
        and the value is another dictionary with 'class', 'params', and 'deps'.

        Example:
            config = {
                'generate_data': {
                    'class': 'my_project.data.NormalDataGenerator',
                    'params': {'mu': 0, 'sigma': 1},
                    'deps': []
                },
                'transform_data': {
                    'class': 'my_project.data.AddNoiseTransformer',
                    'params': {'noise_level': 0.1},
                    'deps': ['generate_data']
                }
            }
        """
        generator = cls()
        for label, step_config in config.items():
            class_path = step_config.get("class")
            params = step_config.get("params", {})
            deps = step_config.get("deps", [])

            if not class_path:
                raise ValueError(f"Missing 'class' for step '{label}' in config.")

            # Dynamically get the class and instantiate it
            step_class = _get_class_from_string(class_path)
            step_instance = step_class(**params)

            # Add the instantiated step to the graph
            generator.add_step(label, step_instance, deps)

        return generator

    def _topological_sort(self) -> List[str]:
        in_degree = {label: 0 for label in self.graph}
        adj = {label: [] for label in self.graph}

        for label, step in self.graph.items():
            for dep in step.deps:
                adj[dep].append(label)
                in_degree[label] += 1

        queue = deque([label for label in self.graph if in_degree[label] == 0])
        sorted_order = []

        while queue:
            u = queue.popleft()
            sorted_order.append(u)

            for v in adj.get(u, []):
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)

        if len(sorted_order) != len(self.graph):
            cycle_nodes = [label for label, degree in in_degree.items() if degree > 0]
            raise ValueError(f"Graph contains a cycle. Involved nodes: {cycle_nodes}")

        return sorted_order

    def generate(
        self, n_samples: int, n_features: int, random_state: int | None = None
    ) -> Tuple[np.ndarray, np.ndarray | None, np.ndarray | None]:
        sorted_labels = self._topological_sort()

        if random_state is not None:
            for label in sorted_labels:
                step = self.graph[label].step
                if hasattr(step, "random_state"):
                    step.random_state = random_state

        results: Dict[str, np.ndarray] = {}
        X: np.ndarray | None = None
        y: np.ndarray | None = None
        theta: np.ndarray | None = None

        for label in sorted_labels:
            step = self.graph[label]
            step_obj = step.step

            dep_outputs = [results[dep_label] for dep_label in step.deps]

            output = None
            if isinstance(step_obj, BaseDataGenerator):
                output = step_obj.generate(n_samples=n_samples, n_features=n_features)
                X = output
            elif isinstance(step_obj, BaseDataTransformer):
                if not dep_outputs:
                    raise ValueError(
                        f"DataTransformer step '{label}' requires a dependency."
                    )
                output = step_obj.transform(dep_outputs[0])
                X = output
            elif isinstance(step_obj, BaseParameterGenerator):
                output = step_obj.generate(n_features=n_features)
                theta = output
            elif isinstance(step_obj, BaseParameterTransformer):
                if not dep_outputs:
                    raise ValueError(
                        f"ParameterTransformer step '{label}' requires a dependency."
                    )
                output = step_obj.transform(dep_outputs[0])
                theta = output
            elif isinstance(step_obj, BaseLabelComputer):
                if len(dep_outputs) < 1:
                    raise ValueError(
                        f"LabelComputer step '{label}' requires at least one dependency."
                    )
                output = step_obj.compute(*dep_outputs)
                y = output
            elif isinstance(step_obj, BaseLabelGenerator):
                output = step_obj.generate(n_samples=n_samples)
                y = output
            else:
                raise TypeError(
                    f"Unknown step type for label '{label}': {type(step_obj)}"
                )

            if output is not None:
                results[label] = output

        if X is None:
            raise RuntimeError("No data generated. This should never run")

        return X, y, theta
