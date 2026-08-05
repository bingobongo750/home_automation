"""Pure control law for auto-lighting. No DB, no Flask, no threads, no clock —
so it unit-tests cheaply against a simulated room (see tests/test_lighting_control.py).
app/lighting.py owns the sensor reads, the bulb pushes and the timing.

WHAT THIS REPLACES, AND WHY IT HAD TO CHANGE
--------------------------------------------
The old job was OPEN-LOOP: brightness = f(lux) on a fixed linear ramp, full
brightness at pitch dark fading to off at a `lux_off` cutoff. That cannot hold
the room at a level, because it never aims at one — it only fades out. And the
mapping is circular: the BH1750 measures TOTAL illuminance, which includes the
light our own bulbs are emitting. Feed that back through a ramp and "brighter
room" and "our bulbs are working" are indistinguishable.

So this is a closed loop with a setpoint: drive the MEASURED lux to
`target_lux` by adjusting brightness, and say plainly when it cannot.

WHO OWNS WHAT: AUTO OWNS BRIGHTNESS, THE USER OWNS ON/OFF
---------------------------------------------------------
This module returns a brightness and nothing else. It has no opinion about
whether a lamp is on, and app/lighting.py never sends the `on` field.

The zone's on/off switch is the user deciding *which lamps may light at all* —
a master gate, not a value for the loop to fight over. It used to be exactly
that fight: the job pushed `on=True` every tick, so switching a zone off while
it was in auto mode simply undid itself seconds later. Off has to win.

The consequence to be aware of: when the loop wants zero light from a zone the
user has left ON, it commands brightness 0, and the transport floors that to
the Shelly's 1 % minimum (the bulb rejects 0 — "off" is only expressible
through `on`, which is no longer ours to send). So a switched-on zone bottoms
out at a faint glow rather than going dark. That is the honest price of the
rule, and it is nearly always invisible: the loop only wants zero light when
ambient already exceeds the target, i.e. when the room is bright enough that
1 % cannot be seen. If a lamp should be truly dark, switch it off — which is
precisely the control this change hands back.

WHY INTEGRAL-ONLY, NOT PID
--------------------------
`brightness -> lux at the sensor` has an unknown gain: it depends on the bulb,
the room's reflectance, and where the sensor sits relative to the lamp. Nobody
is going to measure that, and it changes if the sensor moves.

  * Integral (incremental) control converges to zero steady-state error without
    knowing the plant gain — each tick nudges brightness in the direction that
    reduces the error, and it stops when the error is gone. That is exactly the
    property needed: we must HIT 5 lx, not settle near it.
  * A proportional-only law leaves permanent droop, which is the one thing a
    setpoint controller must not do.
  * No derivative term. Lux arrives as INTEGERS (the firmware prints
    `(long)lux`), so at a 5 lx setpoint the signal is quantised at 20 % of the
    setpoint. Differentiating that amplifies quantisation noise into brightness
    flicker.

Gain is a hand-tuned constant, deliberately NOT self-fitted from observed
Δlux/Δbrightness. An adaptive estimate sounds appealing and is a trap here:
ambient light drifts on its own (cloud, sunset, a door opening), so any
observed Δlux is a mix of "our bulb did that" and "the room changed", and a
gain estimator fed that mixture chases its own tail. Same stance the health
module takes on its weights: tunable, never auto-fitted.

THE FOUR THINGS THAT KEEP IT STABLE
-----------------------------------
1. DEADBAND. Integer lux at a 5 lx target means ±1 lx is ±20 %, and brightness
   itself quantises to the Shelly's 1 % steps. Without a deadband the loop
   hunts forever and you can see it. Inside the band, do nothing.
2. SLEW LIMIT. Cap the change per tick so a big error cannot slam the bulb
   across its range before the next measurement arrives.
3. OVERSHOOT-REACTIVE STEP SCALING — and this one is not optional, it was
   found by the convergence tests rather than by reasoning. A single fixed
   gain CANNOT work here, because the plant gain spans decades: on a dim lamp
   in a big room one brightness unit is worth ~0.05 lx, on a bright lamp beside
   the sensor it is worth ~1 lx or more. With gain 6 and a 16-unit slew cap,
   the strong plant produced a perfect limit cycle: 0 -> 16 units -> 16 lx (way
   over) -> back to 0 -> 0 lx (way under) -> forever, because the smallest step
   the controller would take was coarser than the whole deadband.

   The fix is not to estimate the plant (see above — ambient drift poisons any
   such estimate). It is to react to the symptom: each time the error CHANGES
   SIGN we overshot, so halve the slew cap; while the sign holds, relax it back
   toward full (×1.25) so a real change in the room is still followed briskly.
   Halving beats the growth rate, so repeated overshoot contracts geometrically
   and the loop lands in a handful of ticks on every plant gain tested. This
   costs one float and one int of carried state, which is why `correct()` takes
   and returns `step_scale` / `sign` instead of being a plain function of the
   measurement.

   The cap is reset to full only when a large error appears *after* the loop had
   settled — that is a new disturbance, not the tail of an overshoot. Resetting
   on any large error instead would undo the halving exactly when it is needed,
   since the error is at its largest right after an overshoot.
4. SATURATION IS A REPORTED STATE, NOT A FAILURE. See TOO_BRIGHT below.

app/lighting.py adds the fifth: never correct twice on the same measurement.
"""

from dataclasses import dataclass

# Step-scale dynamics (see point 3 above). Shrink must outpace growth or
# repeated overshoot would not contract.
_SCALE_SHRINK = 0.5
_SCALE_GROW = 1.25
_SCALE_MIN = 0.02          # the `max(1, ...)` on the cap keeps it able to move
# A large error arriving after the loop had settled counts as a new
# disturbance, and re-earns the full slew cap. In multiples of the deadband.
_RESET_ERROR_FACTOR = 4.0

# Controller states. The saturated ones are the whole point of the exercise —
# "the room may already be too bright" is a normal, expected outcome, not an
# error, and the dashboard says so rather than silently doing nothing.
HOLDING = "holding"          # inside the deadband: at target, nothing to do
CONVERGING = "converging"    # moving toward the target
TOO_BRIGHT = "too_bright"    # ambient alone exceeds the target; brightness at 0
AT_MAX = "at_max"            # at the brightness ceiling and still short of target
OFF_BY_SETTING = "off"       # target_lux <= 0: auto mode is switched off
NO_READING = "no_reading"    # no lux sample has ever arrived
STALE = "stale"              # lux samples stopped arriving; loop is blind
# Set by app/lighting.py, not reachable from correct(): every auto zone is
# switched off, so there is nothing for the loop to drive. It holds rather than
# integrating, or it would wind up to full while blind and then blast the room
# the moment a switch went back on.
ZONES_OFF = "zones_off"


@dataclass(frozen=True)
class Correction:
    brightness: int   # 0-255, hub scale, what to command
    state: str        # one of the constants above
    error: float      # target - measured, in lx (0.0 when there is no reading)
    # Carried state — feed both back into the next call. See point 3 in the
    # module docstring; without them the loop limit-cycles on a strong plant.
    step_scale: float = 1.0
    sign: int = 0     # -1 too bright / 0 in band / +1 too dark, last tick


def _clamp(value, low, high):
    return max(low, min(high, value))


def correct(*, measured_lux, target_lux, brightness, max_brightness,
            deadband_lux, gain, max_step, step_scale=1.0, last_sign=0,
            reading_stale=False):
    """One tick of the loop -> the brightness to command now.

    measured_lux    latest BH1750 reading in lx, or None if there is none
    target_lux      the setpoint the user owns (0 disables auto lighting)
    brightness      what we currently have commanded, 0-255 (the integrator)
    max_brightness  ceiling; the controller never exceeds it
    deadband_lux    half-width of the accepted band around the target
    gain            brightness units per lx of error, before the slew limit
    max_step        largest brightness change permitted in one tick, at full
                    scale; the effective cap is this times `step_scale`
    step_scale      carried from the previous Correction (see module docstring)
    last_sign       carried from the previous Correction
    reading_stale   True if the newest reading predates our last change and so
                    cannot describe its effect (app/lighting.py decides this —
                    it is a timing question, and this module has no clock)
    """
    brightness = int(_clamp(brightness, 0, max_brightness))
    step_scale = _clamp(step_scale, _SCALE_MIN, 1.0)

    # A zero/negative target means "auto mode should not light the room" —
    # preserves the old ramp's `lux_off <= 0` escape hatch.
    if target_lux <= 0:
        return Correction(brightness, OFF_BY_SETTING, 0.0, step_scale, 0)

    if measured_lux is None:
        # Never had a reading: light the room rather than leave someone in the
        # dark. This is the pre-existing product decision ("better a lit room
        # than a dark one on sensor loss") and it only applies when we have no
        # idea at all — going to full brightness on a reading that merely went
        # stale could switch the lamp on in broad daylight.
        return Correction(max_brightness, NO_READING, 0.0, step_scale, 0)

    if reading_stale:
        # Blind for now: hold what we have. Correcting on a measurement taken
        # before our last change would double-count it and overshoot.
        return Correction(brightness, STALE, 0.0, step_scale, last_sign)

    error = target_lux - measured_lux          # +ve = too dark, need more light

    if abs(error) <= deadband_lux:
        # Converged. Keep the scale we converged at — the sign logic will open
        # it back up if the room genuinely changes.
        return Correction(brightness, HOLDING, error, step_scale, 0)

    sign = 1 if error > 0 else -1
    if last_sign != 0 and sign != last_sign:
        step_scale = max(_SCALE_MIN, step_scale * _SCALE_SHRINK)   # we overshot
    elif last_sign == 0 and abs(error) > deadband_lux * _RESET_ERROR_FACTOR:
        step_scale = 1.0            # settled, then the room changed a lot
    elif sign == last_sign:
        step_scale = min(1.0, step_scale * _SCALE_GROW)            # keep going

    cap = max(1, int(round(max_step * step_scale)))
    step = int(round(_clamp(gain * error, -cap, cap)))
    if step == 0:
        # Error outside the deadband but the gain rounds to no movement. Move
        # one unit so the loop cannot stall short of the band forever.
        step = sign
    new_brightness = int(_clamp(brightness + step, 0, max_brightness))

    if new_brightness == brightness:
        # Clamped: we are asking for light we cannot add or remove.
        state = AT_MAX if error > 0 else TOO_BRIGHT
        return Correction(brightness, state, error, step_scale, sign)

    return Correction(new_brightness, CONVERGING, error, step_scale, sign)


def describe(state, *, target_lux, measured_lux=None):
    """One-line human explanation, for logs and the dashboard card."""
    lux = "—" if measured_lux is None else f"{measured_lux:.0f} lx"
    if state == OFF_BY_SETTING:
        return "Auto lighting off (target set to 0 lx)."
    if state == ZONES_OFF:
        return "Switched off — auto lighting will not turn it on."
    if state == NO_READING:
        return "No light reading yet — holding full brightness."
    if state == STALE:
        return "Waiting for a fresh light reading."
    if state == TOO_BRIGHT:
        # "at minimum", not "off": the loop no longer switches zones off, it
        # bottoms their brightness out (see the ownership note above).
        return f"Room already brighter than target ({lux} vs {target_lux:.0f} lx) — at minimum."
    if state == AT_MAX:
        return f"At maximum brightness, still below target ({lux} of {target_lux:.0f} lx)."
    if state == HOLDING:
        return f"Holding {target_lux:.0f} lx ({lux})."
    return f"Adjusting toward {target_lux:.0f} lx ({lux})."
