import numpy as np
from abc import ABC, abstractmethod
from typing import Callable, Tuple


class BaseDataGenerator(ABC):
    """
    Abstract base class for generators that produce features and labels together.
    """

    def __init__(self, random_state: int | None = None) -> None:
        self.random_state = random_state

    @abstractmethod
    def generate(self, n_samples: int, **kwargs) -> np.ndarray:
        """
        Generates the base dataset.
        Returns: A tuple containing features (X) and labels (y).
        """
        pass


class BaseDataTransformer(ABC):
    """
    Abstract base class for all data transformers.
    """

    def __init__(self, random_state: int | None = None) -> None:
        self.random_state = random_state

    @abstractmethod
    def transform(self, X: np.ndarray, y: np.ndarray | None = None) -> np.ndarray:
        """
        Applies a transformation to the dataset.
        Returns: The transformed features and labels.
        """
        pass


# ---------------------------------------------------
# GENERATORS
# ---------------------------------------------------


class GaussianDataGenerator(BaseDataGenerator):
    """
    Generates data from a Gaussian distribution.
    """

    def __init__(
        self,
        mean: float,
        std: float,
        random_state: int | None = None,
    ) -> None:
        super().__init__(random_state)
        self.mean = mean
        self.std = std

    def generate(
        self,
        n_samples: int,
        **kwargs,
    ) -> np.ndarray:
        n_features = kwargs.get("n_features", None)
        if n_features == None:
            raise ValueError("n_features must be given")

        rng = np.random.default_rng(self.random_state)
        X = rng.normal(loc=self.mean, scale=self.std, size=(n_samples, n_features))

        return X


# ---------------------------------------------------
# TRANSFORMERS
# ---------------------------------------------------


class GaussianNoiseTransformer(BaseDataTransformer):
    """
    Adds Gaussian noise to the dataset.
    """

    def __init__(
        self,
        std: float,
        random_state: int | None = None,
    ) -> None:
        super().__init__(random_state)
        self.std = std

    def transform(self, X: np.ndarray, y: np.ndarray | None = None) -> np.ndarray:
        rng = np.random.default_rng(self.random_state)
        noise = rng.normal(scale=self.std, size=X.shape)
        return X + noise


class LinearDependentTransformer(BaseDataTransformer):
    """
    Replaces some features with a linear combination
    of other features plus optional noise.
    """

    def __init__(
        self,
        percent_independent: float,
        std: float = 1.0,
        noise_std: float = 0.0,
        random_state: int | None = None,
    ) -> None:
        super().__init__(random_state)
        if not (0 < percent_independent < 1):
            raise ValueError("percent_independent must be between 0 and 1")

        self.percent_independent = percent_independent
        self.std = std
        self.noise_std = noise_std

    def transform(self, X: np.ndarray, y: np.ndarray | None = None) -> np.ndarray:
        _, n_features = X.shape
        n_independent = int(n_features * self.percent_independent)
        n_dependent = n_features - n_independent

        if n_independent == 0 or n_dependent == 0:
            raise ValueError(
                "percent_independent results in no independent or dependent features"
            )

        rng = np.random.default_rng(self.random_state)

        independent_indices = rng.choice(
            n_features,
            size=n_independent,
            replace=False,
        )
        dependent_indices = [
            i for i in range(n_features) if i not in independent_indices
        ]

        coefficients = rng.normal(scale=self.std, size=(n_dependent, n_independent))

        X_dependent = X[:, independent_indices] @ coefficients.T

        if self.noise_std > 0:
            noise = rng.normal(scale=self.noise_std, size=X_dependent.shape)
            X_dependent += noise

        X[:, dependent_indices] = X_dependent

        return X
