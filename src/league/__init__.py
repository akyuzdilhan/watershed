"""A light synthetic league simulated at score level (N1): calibrated club strengths, a full season, an observer's view of the table and the context of the demo fixture."""

from league.calibration import calibrate
from league.coupling import demo_fixture
from league.model import Params
from league.season import League, match_importance, season_state

__all__ = ["League", "Params", "calibrate", "demo_fixture", "match_importance", "season_state"]
