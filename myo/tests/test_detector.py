import pytest

from myo_fall.detector import FallConfig, FallDetector
from myo_fall.recording import detect_in
from myo_fall import sim


@pytest.mark.parametrize("name", [n for n, (_, f) in sim.SCENARIOS.items() if f])
def test_falls_are_detected_once(name):
    build, _ = sim.SCENARIOS[name]
    events, det = detect_in(build())
    assert len(events) == 1, [e.reason for e in det.evaluations]
    assert events[0].confidence >= 0.6
    assert len(events[0].cues) >= 2


@pytest.mark.parametrize("name", [n for n, (_, f) in sim.SCENARIOS.items() if not f])
def test_daily_activities_do_not_alarm(name):
    build, _ = sim.SCENARIOS[name]
    events, det = detect_in(build())
    assert events == [], [(e.reason, e.features) for e in det.evaluations]


def test_fall_followed_by_immediate_recovery_is_not_reported():
    events, det = detect_in(sim.fall_then_gets_up())
    assert events == []
    assert det.evaluations[-1].reason == "moving after impact"


def test_table_slap_alarms_if_one_cue_is_enough():
    """Shows the sensitivity / false-alarm trade-off controlled by min_supporting."""
    events, _ = detect_in(sim.adl_table_slap(), FallConfig(min_supporting=1))
    assert len(events) == 1


def test_event_timestamp_is_the_impact():
    samples = sim.fall_forward_trip()
    events, _ = detect_in(samples)
    peak = max(samples, key=lambda s: sum(v * v for v in s.accel))
    assert abs(events[0].t - peak.t) < 0.1


def test_refractory_period_suppresses_repeat_alarms():
    samples = sim.fall_forward_trip() + _shift(sim.fall_forward_trip(), 13.0)
    events, _ = detect_in(samples, FallConfig(refractory_s=60))
    assert len(events) == 1
    events, _ = detect_in(samples, FallConfig(refractory_s=1))
    assert len(events) == 2


def test_reset_discards_pending_candidate():
    det = FallDetector()
    samples = sim.fall_forward_trip()
    peak_i = max(range(len(samples)), key=lambda i: sum(v * v for v in samples[i].accel))
    for s in samples[: peak_i + 5]:
        det.update(s.accel, s.gyro, s.t)
    assert det.state == FallDetector.CANDIDATE
    det.reset()
    assert det.state == FallDetector.IDLE
    for s in samples[peak_i + 5:]:
        assert det.update(s.accel, s.gyro, s.t) is None


def test_config_overrides():
    cfg = FallConfig.from_overrides({"impact_g": "3.0", "min_supporting": "1"})
    assert cfg.impact_g == 3.0 and cfg.min_supporting == 1 and isinstance(cfg.min_supporting, int)
    with pytest.raises(ValueError):
        FallConfig.from_overrides({"impact": "3"})


def test_handles_impact_right_at_startup():
    """No pre-impact history: must not crash, orientation cue just unavailable."""
    samples = sim.fall_forward_trip()
    peak_i = max(range(len(samples)), key=lambda i: sum(v * v for v in samples[i].accel))
    events, det = detect_in(samples[peak_i - 2:])
    assert det.evaluations and det.evaluations[0].features["orientation_change_deg"] is None


def _shift(samples, dt):
    last = samples[-1].t
    for s in samples:
        s.t = s.t - samples[0].t + last + dt
    return samples
