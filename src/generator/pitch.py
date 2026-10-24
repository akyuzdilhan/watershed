"""Pitch geometry and the shot quality (xG) model.

Coordinates are in metres on a 105 x 68 m pitch. Inside the engine the home team always attacks towards x = 105; the feed turns every location into the frame of the acting team (it attacks towards x = 105, y = 0 on its right), as SPADL does.
"""

import math

LENGTH, WIDTH = 105.0, 68.0
HALF_X, MID_Y = LENGTH / 2, WIDTH / 2
CENTRE = (HALF_X, MID_Y)
GOAL_HALF = 3.66  # posts 7.32 m apart
POSTS = (MID_Y - GOAL_HALF, MID_Y + GOAL_HALF)
CROSSBAR = 2.44
BOX_X, BOX_HALF = LENGTH - 16.5, 20.16  # penalty area at the attacking end
SIX_X, SIX_HALF = LENGTH - 5.5, 9.16
PENALTY_SPOT = (LENGTH - 11.0, MID_Y)


def flip(x: float, y: float) -> tuple[float, float]:
    """The same point seen from the other team."""
    return LENGTH - x, WIDTH - y


def to_frame(side: str, x: float, y: float) -> tuple[float, float]:
    """Engine frame (home attacks towards x = 105) to the frame of `side`."""
    return (x, y) if side == "home" else flip(x, y)


from_frame = to_frame  # the change of frame is its own inverse


def inside(x: float, y: float, margin: float = 0.5) -> tuple[float, float]:
    """Keeps a player position on the pitch (never used for the ball: a ball beyond a line is out of play)."""
    return min(max(x, margin), LENGTH - margin), min(max(y, margin), WIDTH - margin)


def in_box(x: float, y: float) -> bool:
    """Inside the penalty area the attacking team (in its own frame) is facing."""
    return x >= BOX_X and abs(y - MID_Y) <= BOX_HALF


def goal_distance(x: float, y: float) -> float:
    return math.hypot(LENGTH - x, MID_Y - y)


def goal_angle(x: float, y: float) -> float:
    """Angle in radians between the two posts, seen from (x, y)."""
    dx = max(LENGTH - x, 0.05)
    return abs(math.atan2(POSTS[1] - y, dx) - math.atan2(POSTS[0] - y, dx))


def in_cone(shooter: tuple[float, float], point: tuple[float, float]) -> bool:
    """Whether `point` stands in the triangle between the shooter and the posts."""
    (sx, sy), (px, py) = shooter, point
    if not sx < px < LENGTH:
        return False
    t = (px - sx) / (LENGTH - sx)
    low, high = sy + t * (POSTS[0] - sy), sy + t * (POSTS[1] - sy)
    return low - 0.5 <= py <= high + 0.5


# Logistic xG on angle and distance, hand-tuned so that the averages land near Premier League 2024/25 conversion rates (14.7 % in the box, 4.2 % outside, Opta). It is the provider xG carried by the feed, not a fitted model.
XG_INTERCEPT, XG_ANGLE, XG_DISTANCE = -1.55, 1.55, -0.085
XG_HEADER, XG_PER_BLOCKER = -1.0, -0.35
PENALTY_XG = 0.78


def xg(x: float, y: float, header: bool = False, blockers: int = 0) -> float:
    """Probability that a shot from (x, y), in the shooter's frame, is a goal. `blockers` counts outfield defenders in the shooting cone."""
    logit = XG_INTERCEPT + XG_ANGLE * goal_angle(x, y) + XG_DISTANCE * goal_distance(x, y)
    logit += XG_HEADER * header + XG_PER_BLOCKER * min(blockers, 3)
    return 1 / (1 + math.exp(-logit))
