lorenz_init = [-8, 8, 27]


def lorenz(t, x, k=[10, 2.66667, 28]):
    return [
        k[0] * x[1] - k[0] * x[0],
        k[2] * x[0] - x[0] * x[2] - x[1],
        x[0] * x[1] - k[1] * x[2],
    ]
