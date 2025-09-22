import numpy as np
from abc import ABC, abstractmethod
from typing import Tuple, List


class BaseLabelGenerator(ABC):
    """
    Abstract base class for
    """

    def __init__(self, random_state: int | None = None) -> None:
        self.random_state = random_state

    @abstractmethod
    def generate(self, n_samples: int, **kwargs) -> np.ndarray:
        """
        Returns:
        """
        pass


class BaseLabelComputer(ABC):
    """
    Abstract base class for
    """

    def __init__(self, random_state: int | None = None) -> None:
        self.random_state = random_state

    @abstractmethod
    def compute(self, X: np.ndarray, theta: np.ndarray, **kwargs) -> np.ndarray:
        """
        Returns:
        """
        pass


class LinearLabelComputer(BaseLabelComputer):
    def __init__(self, random_state: int | None = None) -> None:
        super().__init__(random_state)

    def compute(self, X: np.ndarray, theta: np.ndarray, **kwargs) -> np.ndarray:
        if X.shape[1] != theta.shape[0]:
            raise ValueError(
                "Number of features must be consistant between X and theta"
            )

        return X @ theta
