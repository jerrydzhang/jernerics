import numpy as np
from abc import ABC, abstractmethod
from typing import Tuple, List, Callable


class BaseParameterGenerator(ABC):
    """
    Abstract base class for
    """

    def __init__(self, random_state: int | None = None) -> None:
        self.random_state = random_state

    @abstractmethod
    def generate(self, n_features: int, **kwargs) -> np.ndarray:
        """
        Generates the base parameters.
        Returns: A tuple containing features (X) and labels (y).
        """
        pass


class BaseParameterTransformer(ABC):
    """
    Abstract base class for all data transformers.
    """

    def __init__(self, random_state: int | None = None) -> None:
        self.random_state = random_state

    @abstractmethod
    def transform(self, theta: np.ndarray, **kwargs) -> np.ndarray:
        """
        Applies a transformation to the dataset.
        Returns: The transformed features and labels.
        """
        pass


class GaussianParametersGenerator(BaseParameterGenerator):
    def __init__(
        self, mean: float, std: float, random_state: int | None = None
    ) -> None:
        super().__init__(random_state)
        self.mean = mean
        self.std = std

    def generate(self, n_features: int, **kwargs) -> np.ndarray:
        rng = np.random.default_rng(self.random_state)
        theta = rng.normal(loc=self.mean, scale=self.std, size=n_features)
        return theta


class SparseParametersTransformer(BaseParameterTransformer):
    def __init__(self, sparsity: float, random_state: int | None = None) -> None:
        super().__init__(random_state)
        self.sparsity = sparsity

    def transform(self, theta: np.ndarray, **kwargs) -> np.ndarray:
        if self.sparsity < 0.0:
            raise ValueError("Sparsity must be non-negative.")

        num_features = theta.shape[0]

        if self.sparsity >= 1.0:
            num_zero = num_features
        else:
            num_zero = self.sparsity * num_features

        rng = np.random.default_rng(self.random_state)
        mask = rng.choice(num_features, int(num_zero), replace=False)

        theta[mask] = 0.0

        return theta


class ExponentialScaledParametersTransformer(BaseParameterTransformer):
    def __init__(
        self,
        exp: float = 2.0,
        scale_factor: float = 1,
        random_state: int | None = None,
    ) -> None:
        super().__init__(random_state)
        if scale_factor <= 0:
            raise ValueError("Scale factor must be positive.")
        self.exp = exp
        self.scale_factor = scale_factor

    def transform(self, theta: np.ndarray, **kwargs) -> np.ndarray:
        # nonzero_theta = theta[theta != 0]
        # nonzero_theta = (
        #     np.log1p(np.abs(nonzero_theta)) * self.scale_factor * np.sign(nonzero_theta)
        # )
        # theta[theta != 0] = nonzero_theta

        # theta = np.log1p(np.abs(theta)) * self.scale_factor * np.sign(theta)
        theta = np.abs(theta**self.exp) * self.scale_factor * np.sign(theta)

        return theta
