"""Checks the L293D pin logic against the wiring schematic using a fake Jetson.GPIO."""

import pytest

from rosey_agent.motors import L293DMotors, Pins, mix, speed_to_duty


class FakePWM:
    def __init__(self, gpio, pin, hz):
        self.gpio, self.pin, self.hz = gpio, pin, hz
        self.duty = None

    def start(self, duty):
        self.duty = duty

    def ChangeDutyCycle(self, duty):
        assert 0 <= duty <= 100
        self.duty = duty
        self.gpio.log.append(("duty", self.pin, duty))

    def stop(self):
        self.duty = None


class FakeGPIO:
    BOARD, OUT, HIGH, LOW = "BOARD", "OUT", 1, 0

    def __init__(self, pwm_pins=(15, 32, 33)):
        self.mode = None
        self.levels = {}
        self.pwms = {}
        self.log = []
        self.cleaned = None
        self.pwm_pins = pwm_pins

    def setwarnings(self, flag):
        pass

    def setmode(self, mode):
        self.mode = mode

    def setup(self, channels, direction, initial=0):
        for ch in channels if isinstance(channels, (list, tuple)) else [channels]:
            self.levels[ch] = initial

    def output(self, ch, level):
        self.levels[ch] = level
        self.log.append(("out", ch, level))

    def PWM(self, pin, hz):
        if pin not in self.pwm_pins:
            raise ValueError("Channel %d is not a PWM" % pin)
        self.pwms[pin] = FakePWM(self, pin, hz)
        return self.pwms[pin]

    def cleanup(self, channels=None):
        self.cleaned = channels


def make(**kw):
    gpio = FakeGPIO()
    return L293DMotors(gpio=gpio, **kw), gpio


def test_pins_match_schematic():
    p = Pins()
    assert (p.left_en, p.left_a, p.left_b) == (15, 29, 31)
    assert (p.right_en, p.right_a, p.right_b) == (33, 35, 37)


def test_setup_starts_everything_off_in_board_mode():
    m, gpio = make()
    assert gpio.mode == "BOARD"
    assert all(gpio.levels[p] == 0 for p in Pins().all())
    assert gpio.pwms[15].duty == 0 and gpio.pwms[33].duty == 0


def test_forward_reverse_and_stop():
    m, gpio = make(min_duty=40, max_duty=100)
    m.set(1.0, -0.5)
    L = gpio.levels
    assert (L[29], L[31]) == (1, 0)          # left forward: 1A high, 2A low
    assert (L[35], L[37]) == (0, 1)          # right reverse: 3A low, 4A high
    assert gpio.pwms[15].duty == 100
    assert gpio.pwms[33].duty == pytest.approx(70)  # 40 + 60 * 0.5
    m.stop()
    assert gpio.pwms[15].duty == 0 and gpio.pwms[33].duty == 0
    assert all(L[p] == 0 for p in (29, 31, 35, 37))
    assert not m.moving


def test_enable_is_off_while_direction_changes():
    m, gpio = make()
    m.set(0.8, 0.8)
    gpio.log.clear()
    m.set(-0.8, 0.8)
    left_events = [e for e in gpio.log if e[1] in (15, 29, 31)]
    assert left_events[0] == ("duty", 15, 0)  # enable off first ...
    assert left_events[-1][0] == "duty" and left_events[-1][2] > 0  # ... back on last
    # the right wheel kept its direction: only its speed was touched
    assert [e for e in gpio.log if e[1] in (35, 37)] == []


def test_invert_flips_direction_pins():
    m, gpio = make(invert_left=True)
    m.set(0.5, 0.5)
    assert (gpio.levels[29], gpio.levels[31]) == (0, 1)
    assert (gpio.levels[35], gpio.levels[37]) == (1, 0)


def test_max_speed_scales_and_clamps():
    m, gpio = make(max_speed=0.5, min_duty=0)
    m.set(3.0, -3.0)
    assert (m.left, m.right) == (0.5, -0.5)
    assert gpio.pwms[15].duty == pytest.approx(50)


def test_falls_back_to_on_off_without_pwm():
    gpio = FakeGPIO(pwm_pins=())
    m = L293DMotors(gpio=gpio)
    m.set(0.3, 0)
    assert gpio.levels[15] == 1 and gpio.levels[33] == 0
    m.stop()
    assert gpio.levels[15] == 0


def test_close_stops_and_cleans_up():
    m, gpio = make()
    m.set(1, 1)
    m.close()
    assert not m.moving and gpio.cleaned == Pins().all()


def test_speed_to_duty_deadband():
    assert speed_to_duty(0.01, 35, 100) == 0
    assert speed_to_duty(0.1, 35, 100) == pytest.approx(41.5)
    assert speed_to_duty(-1.0, 35, 100) == 100


def test_agent_mix_matches_server_mix():
    from server.protocol import mix as server_mix
    for lin, ang in [(1, 0), (0.5, 0.3), (-0.7, 0.9), (0, -1)]:
        assert mix(lin, ang) == server_mix(lin, ang)
