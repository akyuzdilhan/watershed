"""Score-level (N1) match model: club strengths on a log scale, Poisson goals with the Dixon-Coles low-score correction.

log rate of the home side = mu + h + attack[home] + defence[away], log rate of the away side = mu + attack[away] + defence[home]. A high attack scores more, a high defence concedes more. Every function is vectorised over seasons and fixtures.
"""

import functools
import math
from dataclasses import dataclass

import numpy as np

N_CLUBS = 20


@dataclass(frozen=True)
class Params:
    """League parameters. Hand-set starting values, checked against season-level targets (league.calibration), not fitted."""

    sigma: float = 0.30  # standard deviation of attack quality and of defensive solidity, log scale
    correlation: float = 0.8  # between attack quality and defensive solidity
    home_ratio: float = 1.25  # home scoring rate over away scoring rate
    rho: float = -0.10  # Dixon-Coles correction: more 0-0 and 1-1, fewer 1-0 and 0-1
    goals_per_match: float = 2.85  # league average, both sides: Premier League 2022/23 (2.85) and 2024/25 (2.87)
    elite: bool = False  # elite tier: the strongest clubs pulled further clear, on both attack and defence
    elite_clubs: int = 2
    elite_shift: float = 0.13
    elite_sigma: float = 0.28  # spread of the other clubs when the elite tier is on

    def __post_init__(self) -> None:
        if not (0 < self.sigma and -1 < self.correlation < 1 and self.home_ratio > 0 and self.goals_per_match > 0 and 0 <= self.elite_clubs <= N_CLUBS):
            raise ValueError(f"invalid league parameters: {self}")

    @property
    def spread(self) -> float:
        return self.elite_sigma if self.elite else self.sigma

    def base(self, n_clubs: int = N_CLUBS) -> tuple[float, float]:
        """mu and h, so that a fixture between two clubs drawn at random averages `goals_per_match`.

        Jensen's effect: E[exp(a + d)] > exp(E[a + d]), so the base rate is lowered by the mean of exp(attack) and exp(defence) over the strength distribution: the lognormal factor exp(sigma^2 / 2) without the elite tier, a fixed-seed Monte Carlo estimate with it (the elite clubs are the top of the draw, not a random pick).
        """
        mean_exp_attack, mean_exp_defence = _mean_exp(self, n_clubs)
        away = self.goals_per_match / (1 + self.home_ratio)
        return math.log(away) - math.log(mean_exp_attack * mean_exp_defence), math.log(self.home_ratio)


@functools.lru_cache(maxsize=32)
def _mean_exp(params: Params, n_clubs: int) -> tuple[float, float]:
    if not (params.elite and params.elite_clubs):
        f = math.exp(params.sigma**2 / 2)
        return f, f
    attack, defence = draw_strengths(np.random.default_rng(0), params, (20_000,), n_clubs)
    return float(np.exp(attack).mean()), float(np.exp(defence).mean())


def draw_strengths(rng: np.random.Generator, params: Params, shape: tuple[int, ...] = (), n_clubs: int = N_CLUBS) -> tuple[np.ndarray, np.ndarray]:
    """Attack and defence of `n_clubs` clubs, arrays of shape `shape + (n_clubs,)`, from a bivariate normal on (attack quality, defensive solidity).

    With the elite tier, the `elite_clubs` best clubs (attack plus solidity) gain `elite_shift` on both, which opens a gap at the top of the strength distribution.
    """
    s, r = params.spread, params.correlation
    z1 = rng.standard_normal((*shape, n_clubs))
    z2 = rng.standard_normal((*shape, n_clubs))
    attack = s * z1
    solidity = s * (r * z1 + math.sqrt(1 - r * r) * z2)
    if params.elite and params.elite_clubs:
        elite = np.argsort(-(attack + solidity), axis=-1)[..., : params.elite_clubs]
        bonus = np.zeros((*shape, n_clubs))
        np.put_along_axis(bonus, elite, params.elite_shift, axis=-1)
        attack, solidity = attack + bonus, solidity + bonus
    return attack, -solidity


def rates(params: Params, attack: np.ndarray, defence: np.ndarray, home: np.ndarray, away: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Expected goals of the home and away sides for fixtures (home[k], away[k]); strengths have clubs on their last axis."""
    mu, h = params.base(attack.shape[-1])
    lam_home = np.exp(mu + h + attack[..., home] + defence[..., away])
    lam_away = np.exp(mu + attack[..., away] + defence[..., home])
    return lam_home, lam_away


def dc_tau(x: np.ndarray, y: np.ndarray, lam: np.ndarray, mu: np.ndarray, rho: float) -> np.ndarray:
    """Dixon-Coles weight of the score x-y."""
    tau = np.ones(np.broadcast_shapes(x.shape, lam.shape))
    tau = np.where((x == 0) & (y == 0), 1 - lam * mu * rho, tau)
    tau = np.where((x == 0) & (y == 1), 1 + lam * rho, tau)
    tau = np.where((x == 1) & (y == 0), 1 + mu * rho, tau)
    tau = np.where((x == 1) & (y == 1), 1 - rho, tau)
    return np.maximum(tau, 0.0)  # with rho < 0, a side expected to score more than 1 / -rho goals can no longer lose 1-0


def sample_scores(rng: np.random.Generator, lam_home: np.ndarray, lam_away: np.ndarray, rho: float) -> tuple[np.ndarray, np.ndarray]:
    """Scores from independent Poisson goals, reweighted by Dixon-Coles through rejection sampling: a draw is kept with probability tau / max tau."""
    lam, mu = np.broadcast_arrays(np.asarray(lam_home, float), np.asarray(lam_away, float))
    shape = lam.shape
    lam, mu = lam.ravel(), mu.ravel()
    bound = np.max([np.ones_like(lam), 1 - rho * lam * mu, 1 + rho * lam, 1 + rho * mu, np.full_like(lam, 1 - rho)], axis=0)
    x, y = rng.poisson(lam), rng.poisson(mu)
    todo = np.arange(lam.size)
    while rho != 0 and todo.size:
        keep = rng.random(todo.size) * bound[todo] < dc_tau(x[todo], y[todo], lam[todo], mu[todo], rho)
        todo = todo[~keep]
        x[todo], y[todo] = rng.poisson(lam[todo]), rng.poisson(mu[todo])
    return x.reshape(shape), y.reshape(shape)
