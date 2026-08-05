"""Auto-lighting control law (app/lighting_control.py) — pure, so these run
without a DB, a bulb or a clock.

The interesting tests are the closed-loop ones at the bottom: they wire the
controller to a simulated room and check it actually settles, because "each
tick moves in the right direction" is not the same claim as "it converges and
then stops". A control loop that passes unit tests and oscillates in the room
is the failure mode worth spending test code on.
"""

import os
import unittest

os.environ.setdefault("MOCK_HARDWARE", "1")

from app import lighting_control as lc


DEFAULTS = dict(target_lux=5.0, max_brightness=180, deadband_lux=1.0,
                gain=6.0, max_step=16)


def correct(**kwargs):
    return lc.correct(**{**DEFAULTS, **kwargs})


class TestStates(unittest.TestCase):
    def test_at_target_holds(self):
        r = correct(measured_lux=5.0, brightness=40)
        self.assertEqual(r.state, lc.HOLDING)
        self.assertEqual(r.brightness, 40)

    def test_inside_deadband_holds(self):
        for lux in (4.0, 4.5, 5.5, 6.0):
            r = correct(measured_lux=lux, brightness=40)
            self.assertEqual(r.state, lc.HOLDING, f"{lux} lx should be in band")
            self.assertEqual(r.brightness, 40)

    def test_too_dark_raises_brightness(self):
        r = correct(measured_lux=0.0, brightness=20)
        self.assertEqual(r.state, lc.CONVERGING)
        self.assertGreater(r.brightness, 20)

    def test_too_bright_lowers_brightness(self):
        r = correct(measured_lux=30.0, brightness=100)
        self.assertEqual(r.state, lc.CONVERGING)
        self.assertLess(r.brightness, 100)

    def test_slew_limit_caps_the_step(self):
        # a 5 lx error at gain 6 asks for 30 units; max_step must cap it
        r = correct(measured_lux=0.0, brightness=0)
        self.assertEqual(r.brightness, DEFAULTS["max_step"])

    def test_never_exceeds_ceiling(self):
        r = correct(measured_lux=0.0, brightness=175)
        self.assertLessEqual(r.brightness, 180)

    def test_never_goes_below_zero(self):
        r = correct(measured_lux=500.0, brightness=5)
        self.assertEqual(r.brightness, 0)

    # --- the "sometimes it cannot" cases the user asked to be handled ---
    def test_room_already_too_bright_reports_saturation(self):
        """Daylight: bulbs off and the room is still over target. Not an error."""
        r = correct(measured_lux=400.0, brightness=0)
        self.assertEqual(r.state, lc.TOO_BRIGHT)
        self.assertEqual(r.brightness, 0)
        self.assertIn("brighter than target", lc.describe(r.state, target_lux=5.0,
                                                          measured_lux=400.0))

    def test_bulb_too_weak_reports_at_max(self):
        r = correct(measured_lux=1.0, brightness=180)
        self.assertEqual(r.state, lc.AT_MAX)
        self.assertEqual(r.brightness, 180)
        self.assertIn("still below target", lc.describe(r.state, target_lux=5.0,
                                                        measured_lux=1.0))

    def test_saturation_does_not_wind_up(self):
        """Held against the ceiling for many ticks, then the room darkens: the
        controller must respond immediately, not work off accumulated error."""
        b = 180
        for _ in range(50):
            b = correct(measured_lux=1.0, brightness=b).brightness
        self.assertEqual(b, 180)
        r = correct(measured_lux=20.0, brightness=b)   # someone opened a blind
        self.assertEqual(r.state, lc.CONVERGING)
        self.assertEqual(r.brightness, 180 - DEFAULTS["max_step"])

    # --- degenerate inputs ---
    def test_zero_target_disables(self):
        """Reports 'off' and leaves brightness alone — app/lighting.py makes no
        push at all in this state, because with the loop not owning `on`,
        "disabled" has to mean hands off rather than 'dim everything to 1 %'."""
        r = correct(measured_lux=0.0, brightness=100, target_lux=0.0)
        self.assertEqual(r.state, lc.OFF_BY_SETTING)
        self.assertEqual(r.brightness, 100)

    def test_no_reading_lights_the_room(self):
        """Pre-existing product decision: better a lit room than a dark one."""
        r = correct(measured_lux=None, brightness=0)
        self.assertEqual(r.state, lc.NO_READING)
        self.assertEqual(r.brightness, 180)

    def test_stale_reading_holds_rather_than_guessing(self):
        r = correct(measured_lux=400.0, brightness=42, reading_stale=True)
        self.assertEqual(r.state, lc.STALE)
        self.assertEqual(r.brightness, 42)

    def test_tiny_error_still_moves_one_step(self):
        """Outside the deadband but the gain rounds to nothing — must not stall."""
        r = correct(measured_lux=3.0, brightness=50, gain=0.001)
        self.assertEqual(r.brightness, 51)


class Room:
    """Simulated room: measured lux = ambient + gain_per_unit * brightness.

    `quantise` reproduces the firmware truncating lux to a whole number
    (`Serial.println((long)lux)`), which is the noise source the deadband
    exists for — testing against smooth floats would hide hunting.
    """

    def __init__(self, ambient, lux_per_unit, quantise=True):
        self.ambient = ambient
        self.lux_per_unit = lux_per_unit
        self.quantise = quantise

    def lux(self, brightness):
        value = self.ambient + self.lux_per_unit * brightness
        return float(int(value)) if self.quantise else value


def run_loop(room, ticks=60, brightness=0, carry_state=True, **kwargs):
    """-> (brightness history, final Correction). One measurement per tick,
    which is what the settle guard in app/lighting.py guarantees.

    carry_state=False pins step_scale at 1.0 every tick, i.e. simulates the
    controller WITHOUT overshoot-reactive scaling — used to show the limit
    cycle that scaling exists to remove.
    """
    history = []
    result = None
    scale, sign = 1.0, 0
    for _ in range(ticks):
        result = correct(measured_lux=room.lux(brightness), brightness=brightness,
                         step_scale=scale, last_sign=sign, **kwargs)
        brightness = result.brightness
        if carry_state:
            scale, sign = result.step_scale, result.sign
        history.append(brightness)
    return history, result


class TestConvergence(unittest.TestCase):
    def assert_settles(self, room, history, result, target=5.0, band=1.0):
        final = room.lux(history[-1])
        self.assertLessEqual(
            abs(final - target), band,
            f"settled at {final} lx, outside {target}+/-{band}; tail={history[-8:]}")
        self.assertEqual(result.state, lc.HOLDING, f"tail={history[-8:]}")
        # and it must be STILL, not orbiting the target
        self.assertEqual(len(set(history[-5:])), 1,
                         f"brightness still moving at the end: {history[-8:]}")

    def test_dark_room_reaches_target(self):
        room = Room(ambient=0.0, lux_per_unit=0.30)   # 180 units -> 54 lx
        history, result = run_loop(room)
        self.assert_settles(room, history, result)

    def test_converges_across_three_decades_of_plant_gain(self):
        """The gain from brightness to lux is unknown and unmeasured — it
        depends on bulb, room and where the sensor sits. The loop must land on
        target anyway; that is the whole reason it is integral."""
        for lux_per_unit in (0.05, 0.1, 0.3, 1.0, 3.0):
            room = Room(ambient=0.0, lux_per_unit=lux_per_unit)
            history, result = run_loop(room, ticks=120)
            with self.subTest(lux_per_unit=lux_per_unit):
                self.assert_settles(room, history, result)

    def test_dim_ambient_is_topped_up(self):
        room = Room(ambient=2.0, lux_per_unit=0.30)
        history, result = run_loop(room)
        self.assert_settles(room, history, result)

    def test_bright_room_switches_off_and_stays_off(self):
        room = Room(ambient=300.0, lux_per_unit=0.30)
        history, result = run_loop(room, brightness=120)
        self.assertEqual(history[-1], 0)
        self.assertEqual(result.state, lc.TOO_BRIGHT)

    def test_follows_a_sunset_without_oscillating(self):
        """Ambient falls from daylight to dark over the run: the loop should be
        off at the start, ramp up as it darkens, and hold at the end."""
        room = Room(ambient=200.0, lux_per_unit=0.30)
        brightness, states = 0, []
        scale, sign = 1.0, 0
        for step in range(120):
            room.ambient = max(0.0, 200.0 - step * 4)
            result = correct(measured_lux=room.lux(brightness), brightness=brightness,
                             step_scale=scale, last_sign=sign)
            brightness = result.brightness
            scale, sign = result.step_scale, result.sign
            states.append(result.state)
        self.assertEqual(states[0], lc.TOO_BRIGHT)
        self.assertLessEqual(abs(room.lux(brightness) - 5.0), 1.0,
                             f"ended at {room.lux(brightness)} lx")
        self.assertEqual(states[-1], lc.HOLDING)

    def test_weak_bulb_ends_at_max_not_hunting(self):
        room = Room(ambient=0.0, lux_per_unit=0.005)   # 180 units -> 0.9 lx
        history, result = run_loop(room, ticks=80)
        self.assertEqual(history[-1], 180)
        self.assertEqual(result.state, lc.AT_MAX)
        self.assertEqual(len(set(history[-5:])), 1)

    def test_strong_plant_limit_cycles_without_step_scaling(self):
        """The regression this whole mechanism exists for, pinned as a test.

        On a strong plant (1 brightness unit ~ 1 lx) a fixed 16-unit slew cap
        is coarser than the entire deadband, so every step vaults over the
        target: 0 -> 16 units -> 16 lx -> 0 -> 0 lx, forever. With
        overshoot-reactive scaling the same room settles.
        """
        room = Room(ambient=0.0, lux_per_unit=1.0)
        fixed, _ = run_loop(room, ticks=40, carry_state=False)
        self.assertGreater(len(set(fixed[-8:])), 1,
                           f"expected a limit cycle without scaling, got {fixed[-8:]}")

        scaled, result = run_loop(room, ticks=40)
        self.assertEqual(result.state, lc.HOLDING, f"tail={scaled[-8:]}")
        self.assertEqual(len(set(scaled[-5:])), 1, f"tail={scaled[-8:]}")

    def test_step_scale_recovers_after_settling(self):
        """A settled loop must not stay stuck on a tiny slew cap — otherwise
        the first real change in the room is followed at a crawl."""
        room = Room(ambient=0.0, lux_per_unit=1.0)
        history, result = run_loop(room, ticks=40)
        self.assertLess(result.step_scale, 1.0)   # shrank while converging
        # now a big disturbance arrives after settling
        room.ambient = 60.0
        nxt = correct(measured_lux=room.lux(history[-1]), brightness=history[-1],
                      step_scale=result.step_scale, last_sign=result.sign)
        self.assertEqual(nxt.step_scale, 1.0, "a new disturbance should re-earn the full cap")

    def test_target_change_is_followed(self):
        room = Room(ambient=0.0, lux_per_unit=0.30)
        history, settled = run_loop(room, ticks=60)
        brightness = history[-1]
        scale, sign = settled.step_scale, settled.sign
        result = None
        for _ in range(80):
            result = correct(measured_lux=room.lux(brightness), brightness=brightness,
                             target_lux=20.0, step_scale=scale, last_sign=sign)
            brightness = result.brightness
            scale, sign = result.step_scale, result.sign
        self.assertLessEqual(abs(room.lux(brightness) - 20.0), 1.0)
        self.assertEqual(result.state, lc.HOLDING)


if __name__ == "__main__":
    unittest.main()
