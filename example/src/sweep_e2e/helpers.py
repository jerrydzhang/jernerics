import os


def fake_loss(lr: float, dropout: float, seed: int) -> float:
    import numpy as np

    rng = np.random.RandomState(seed)
    loss = (np.log10(lr) + 3.0) ** 2 + (1.0 - dropout) ** 2 + rng.normal(0, 0.01)
    return max(loss, 0.0)


def crash_app(trial_number: int, crash_on: list[int]) -> bool:
    return trial_number in crash_on


def crash_node(
    trial_number: int,
    crash_on: list[int],
    lr: float = 0,
    lr_fatal: float = -1,
) -> bool:
    if trial_number in crash_on:
        os._exit(9)
    if lr_fatal > 0 and lr < lr_fatal:
        os._exit(9)
    return False
