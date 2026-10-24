"""Rarity and ranking of a fact: how often it happened in the history, in bits, and its value on the desk.

- Frequency: `k` occurrences over `n` opportunities, with Laplace smoothing, p = (k + 1) / (n + 2), so a fact never seen gets a small, finite p that shrinks as the history grows, and a fact seen every time stays below 1.
- Surprisal: -log2(p), in bits. One more bit means twice as rare.
- Value, 0 to 100: 75 % surprisal (capped at `FULL_BITS`, about 1 in 4,000) and 25 % relevance to the result (the win-probability swing of the moment the fact is tied to, or the weight of a result fact at full time).

Facts measured against the match itself rather than the history (goals against xG, shots against the pre-match share) get a model probability (Poisson or binomial tail) instead of a count; their `basis` says which model.
"""

import math

FULL_BITS = 12.0
SURPRISAL_WEIGHT = 0.75


def laplace(k: int, n: int) -> float:
    return (k + 1) / (n + 2)


def bits(p: float) -> float:
    return -math.log2(max(p, 1e-12))


def value(surprisal_bits: float, relevance: float) -> int:
    s = min(max(surprisal_bits, 0.0) / FULL_BITS, 1.0)
    r = min(max(relevance, 0.0), 1.0)
    return round(100 * (SURPRISAL_WEIGHT * s + (1 - SURPRISAL_WEIGHT) * r))


def one_in(p: float) -> int:
    return max(1, round(1 / p))


def poisson_sf(k: int, mu: float) -> float:
    """P(X >= k) for X ~ Poisson(mu)."""
    if k <= 0:
        return 1.0
    term = math.exp(-mu)
    cdf = term
    for i in range(1, k):
        term *= mu / i
        cdf += term
    return max(1.0 - cdf, 0.0)


def binom_sf(k: int, n: int, q: float) -> float:
    """P(X >= k) for X ~ Binomial(n, q)."""
    return sum(math.comb(n, i) * q**i * (1 - q) ** (n - i) for i in range(k, n + 1))


def normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def fmt(n: int) -> str:
    return f"{n:,}"
