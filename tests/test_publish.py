"""Replay pacing: the minimum real delay between a goal and its VAR check (`--min-var-delay`), on top of the existing pacing."""

from pathlib import Path

import pytest

from generator import publish
from generator.publish import load, schedule

DEMO_FILE = Path(__file__).parent.parent / "data" / "replay" / "demo-0007.jsonl.gz"


@pytest.fixture(scope="module")
def stream():
    return load(DEMO_FILE)


def first_sent(stream: list[dict], times: list[float]) -> dict[str, float]:
    at: dict[str, float] = {}
    for m, t in zip(stream, times):
        at.setdefault(m["message_id"], t)
    return at


def message(i: int, t: float, kind: str, **extra) -> dict:
    return {"message_id": f"m{i}", "type": kind, "emitted_at_s": t, "period": 1, "clock_s": t, **extra}


def test_the_delay_counts_from_the_goal_in_real_time():
    msgs = [
        message(0, 0.0, "action", shot=None),
        message(1, 100.0, "action", shot={"outcome": "goal", "xg": 0.3}, event_id="m1"),
        message(2, 112.0, "var_check", status="started", target_event_id="m1"),
        message(3, 150.0, "var_check", status="completed", target_event_id="m1", decision="stands"),
    ]
    plain = schedule(msgs, speed=10)
    assert plain[2] - plain[1] == pytest.approx(12.0)  # a key moment plays in real time
    delayed = schedule(msgs, speed=10, min_var_delay=30)
    assert delayed[:2] == plain[:2]
    assert delayed[2] - delayed[1] == pytest.approx(30.0)
    assert delayed[3] - delayed[2] == pytest.approx(38.0), "the check itself keeps its length"
    assert schedule(msgs, speed=10, min_var_delay=5) == plain, "a delay shorter than the feed's changes nothing"
    assert schedule(msgs, speed=0, min_var_delay=30) == [0.0] * 4


def test_on_the_demo_every_var_check_waits_for_its_goal(stream):
    plain = schedule(stream, speed=10)
    assert schedule(stream, speed=10, min_var_delay=0) == plain, "no extra wait by default"
    delayed = schedule(stream, speed=10, min_var_delay=45)
    checks = [(i, m) for i, m in enumerate(stream) if m["type"] == "var_check"]
    assert checks, "the demo replay has VAR checks"
    at_plain, at = first_sent(stream, plain), first_sent(stream, delayed)
    assert min(plain[i] - at_plain[m["target_event_id"]] for i, m in checks) < 45, "without the option a check comes sooner"
    for i, m in checks:
        assert delayed[i] - at[m["target_event_id"]] >= 45
    for i, m in enumerate(stream):
        if m["type"] == "correction" and m["target_event_id"] in at:
            assert delayed[i] - at[m["target_event_id"]] >= 40, "the correction delay still holds"
    assert all(b >= a for a, b in zip(delayed, delayed[1:])), "messages still go out in file order"
    assert all(d >= p for p, d in zip(plain, delayed)), "the option only adds waiting"
    started = {m["check_id"]: i for i, m in checks if m["status"] == "started"}
    for i, m in checks:
        if m["status"] == "completed":
            j = started[m["check_id"]]
            assert delayed[i] - delayed[j] == pytest.approx(plain[i] - plain[j]), "a check is not shortened"


@pytest.mark.parametrize(("extra", "expected"), [([], 0.0), (["--min-var-delay", "45"], 45.0)])
def test_the_option_reaches_the_pacing(monkeypatch, extra, expected):
    seen = {}
    monkeypatch.setattr(publish, "replay", lambda messages, send, speed, **pacing: seen.update(pacing))
    monkeypatch.setattr("sys.argv", ["publish", str(DEMO_FILE), "--dry", "--speed", "0", *extra])
    publish.main()
    assert seen["min_var_delay"] == expected and seen["min_correction_delay"] == 40.0
