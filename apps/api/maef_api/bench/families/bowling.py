"""Bowling-score kata family, perturbed by seed.

Canonical (noise 0): 10 pins, 10 frames, strike bonus = next 2 rolls, spare bonus = next 1 roll, module `bowling`,
function `score(rolls)`. Higher noise changes the numbers and the names. The reference below defines the truth for
every variant; the hidden cases are computed from it.
"""
import json
import random

from .base import JUDGED_BY, Variant

CANONICAL = {"pins": 10, "frames": 10, "strike_bonus_rolls": 2, "spare_bonus_rolls": 1}
CANONICAL_NAMES = {"module": "bowling", "function": "score"}
MODULES = ["skittles", "tenpin", "lanes", "alley", "pinfall"]
FUNCTIONS = ["total", "tally", "final_score", "game_score"]


def reference_score(rolls, pins, frames, strike_bonus, spare_bonus) -> int:
    """Score a complete game or raise ValueError. Final frame rules:
    strike -> pins reset, then `strike_bonus` fill balls; spare -> pins reset, then `spare_bonus` fill balls;
    open -> two balls, no fill. Pins reset whenever all pins are down (after a strike or a clear within fill balls).
    """
    if not isinstance(rolls, (list, tuple)):
        raise ValueError("rolls must be a list")  # noqa: TRY004
    for r in rolls:
        if not isinstance(r, int) or isinstance(r, bool):
            raise ValueError("rolls must be integers")  # noqa: TRY004
        if r < 0:
            raise ValueError("negative roll")
        if r > pins:
            raise ValueError("roll exceeds pins")
    n, i = len(rolls), 0
    starts: list[tuple[int, str]] = []  # (index of first roll, kind) for every frame before the last

    def take(limit: int) -> int:
        nonlocal i
        if i >= n:
            raise ValueError("incomplete game")
        r = rolls[i]
        if r > limit:
            raise ValueError("roll exceeds pins standing")
        i += 1
        return r

    total_last = 0
    for f in range(frames):
        start = i
        a = take(pins)
        if f < frames - 1:
            if a == pins:
                starts.append((start, "strike"))
            else:
                b = take(pins - a)
                starts.append((start, "spare" if a + b == pins else "open"))
            continue
        # final frame
        standing = pins - a
        if a == pins:
            standing, fills = pins, strike_bonus
        else:
            b = take(standing)
            standing -= b
            fills = spare_bonus if standing == 0 else 0
            if standing == 0:
                standing = pins
        for _ in range(fills):
            standing -= take(standing)
            if standing == 0:
                standing = pins
        total_last = sum(rolls[start:i])
    if i != n:
        raise ValueError("too many rolls")
    total = total_last
    for start, kind in starts:
        if kind == "strike":
            total += pins + sum(rolls[start + 1:start + 1 + strike_bonus])
        elif kind == "spare":
            total += pins + sum(rolls[start + 2:start + 2 + spare_bonus])
        else:
            total += rolls[start] + rolls[start + 1]
    return total


def _p(v: Variant) -> tuple:
    p = v.params
    return p["pins"], p["frames"], p["strike_bonus_rolls"], p["spare_bonus_rolls"]


def _random_game(rng: random.Random, pins, frames, sb, pb) -> list[int]:
    """Draw a valid game with the same pin-reset rules as the reference (checked by the reference in self-check)."""
    rolls: list[int] = []
    mode = rng.choice(["mixed", "strikey", "sparey", "lowscore"])
    w_strike = {"mixed": 0.2, "strikey": 0.55, "sparey": 0.05, "lowscore": 0.02}[mode]
    w_spare = {"mixed": 0.25, "strikey": 0.1, "sparey": 0.7, "lowscore": 0.05}[mode]

    def ball(standing: int) -> int:
        if rng.random() < w_strike:
            return standing
        if mode == "lowscore":
            return rng.randint(0, max(0, standing // 3))
        return rng.randint(0, standing)

    for f in range(frames):
        a = ball(pins)
        rolls.append(a)
        if f < frames - 1:
            if a < pins:
                rolls.append(_second(rng, a, pins, w_spare, mode))
            continue
        if a == pins:
            standing, fills = pins, sb
        else:
            b = _second(rng, a, pins, w_spare, mode)
            rolls.append(b)
            standing = pins - a - b
            fills = pb if standing == 0 else 0
            standing = pins if standing == 0 else standing
        for _ in range(fills):
            r = ball(standing)
            rolls.append(r)
            standing -= r
            standing = pins if standing == 0 else standing
    return rolls


def _second(rng, a, pins, w_spare, mode) -> int:
    if rng.random() < w_spare:
        return pins - a
    top = pins - a
    if mode == "lowscore":
        return rng.randint(0, top // 3)
    return rng.randint(0, top)


class Bowling:
    id = "bowling"

    def sample(self, seed: int, noise: int) -> Variant:
        rng = random.Random(f"bowling:{seed}:{noise}")
        params, names = dict(CANONICAL), dict(CANONICAL_NAMES)
        if noise >= 1:
            params["pins"] = rng.randint(5, 15)
            params["frames"] = rng.randint(4, 12)
            names = {"module": rng.choice(MODULES), "function": rng.choice(FUNCTIONS)}
        if noise >= 2:
            params["strike_bonus_rolls"] = rng.randint(1, 3)
            params["spare_bonus_rolls"] = rng.randint(1, 2)
        return Variant(family=self.id, seed=seed, noise=noise, params=params, names=names)

    def deliverables(self, v: Variant) -> list[str]:
        m = v.names["module"]
        return [f"{m}.py", f"test_{m}.py", "NOTES.md"]

    def render_task(self, v: Variant) -> str:
        pins, frames, sb, pb = _p(v)
        m, fn = v.names["module"], v.names["function"]
        return f"""# Task: score a game of bowling

## Context

You are scoring a bowling game with slightly unusual rules. Read them carefully: they may differ from the
bowling you know. A game has **{frames} frames**. Every frame starts with **{pins} pins** standing. You have no
network and no pytest; only Python 3 and its standard library are available in your sandbox.

## Requirements

R1. Write a function `{fn}(rolls: list[int]) -> int` in a module named `{m}`. `rolls` is the list of pins knocked
    down by each ball, in order, for exactly one complete game. It returns the total score.
R2. Frames 1 to {frames - 1}: a player gets up to two balls per frame. Knocking down all {pins} pins with the first
    ball is a **strike** and the frame ends. Knocking down all {pins} pins with two balls is a **spare**. Anything
    else is an **open** frame.
R3. An open frame scores the pins knocked down in it.
R4. A strike scores {pins} plus the pins knocked down by the next **{sb}** ball(s) (the following frames' balls,
    counted in order).
R5. A spare scores {pins} plus the pins knocked down by the next **{pb}** ball(s).
R6. If fewer balls were thrown after a frame than its bonus asks for, only the balls that exist count.
R7. Frame {frames} (the last): the player throws a first ball. After a strike the pins reset and the player gets
    **{sb}** extra "fill" ball(s). Otherwise the player throws a second ball; if that clears all pins (a spare) the
    pins reset and the player gets **{pb}** fill ball(s); otherwise the frame ends with no fill balls.
    In the last frame, pins are reset to {pins} whenever all pins are down (after a strike, or after a fill ball
    that clears the pins standing). A fill ball may not knock down more pins than are standing.
R8. The last frame scores just the sum of its own balls, with no bonus on top. Its balls can still serve as bonus
    balls for earlier frames.
R9. Raise `ValueError` for invalid input: a negative roll, a roll larger than the pins standing at that moment,
    too few rolls (the game is not finished), or too many rolls (extra rolls after the game is over). A valid
    `rolls` list is exactly one complete game, nothing more or less. An empty list is invalid.

## Deliverables

D1. `/workspace/out/{m}.py` containing `{fn}`.
D2. `/workspace/out/test_{m}.py`: your own tests, using the standard library `unittest` module (pytest is NOT
    available).
D3. `/workspace/out/NOTES.md`: your approach, your assumptions, and how to run the tests:
    `cd /workspace/out && python3 -m unittest -v`.

## How this will be judged

{JUDGED_BY}
"""

    def hidden_cases(self, v: Variant) -> list[dict]:
        pins, frames, sb, pb = _p(v)
        args = (pins, frames, sb, pb)
        rng = random.Random(f"bowling-cases:{v.seed}:{v.noise}")
        cases: list[tuple[str, list[int]]] = []
        h = pins // 2
        cases.append(("gutter game", [0] * (2 * frames)))
        cases.append(("all ones", [1] * (2 * frames)))
        cases.append(("single spare", [h, pins - h, 3] + [0] * (2 * (frames - 1) - 1)))
        cases.append(("single spare then strike", [h, pins - h, pins] + [0] * (2 * (frames - 2))))
        cases.append(("single strike", [pins, 2, 2] + [0] * (2 * (frames - 2))))
        cases.append(("perfect game", [pins] * (frames - 1 + 1 + sb)))
        cases.append(("all spares", [h, pins - h] * (frames - 1) + [h, pins - h] + [h] * pb))
        cases.append(("all spares, fill 1", [h, pins - h] * frames + [1] * pb))
        cases.append(("strike then spares", [pins] + [h, pins - h] * (frames - 2) + [h, pins - h] + [h] * pb))
        cases.append(("last frame open", [pins] * (frames - 1) + [1, 1]))
        cases.append(("last frame strike, gutter fills", [0] * (2 * (frames - 1)) + [pins] + [0] * sb))
        cases.append(("last frame spare, max fill", [0] * (2 * (frames - 1)) + [h, pins - h] + [pins] * 1 + [0] * (pb - 1)))
        cases.append(("strike then open", [pins, 1, 2] + [0] * (2 * (frames - 2))))
        for k in range(24):
            cases.append((f"random game {k}", _random_game(rng, *args)))
        out: list[dict] = []
        for name, rolls in cases:
            try:
                expected = reference_score(rolls, *args)
            except ValueError as exc:  # a malformed hand-built case is a bug in this generator, not in the agent
                raise AssertionError(f"case {name!r} invalid: {exc}") from exc
            out.append({"name": name, "rolls": rolls, "expected": expected})
        full = _random_game(rng, *args)
        perfect = [pins] * (frames + sb)
        invalid = [
            ("invalid: empty", []),
            ("invalid: negative roll", [-1] + [0] * (2 * frames - 1)),
            ("invalid: roll above pins", [pins + 1] + [0] * (2 * frames)),
            ("invalid: open frame over pins", [pins - 1, 2] + [0] * (2 * frames - 2)),
            ("invalid: too few rolls", perfect[:-1]),
            ("invalid: too few, gutter", [0] * (2 * frames - 1)),
            ("invalid: too many rolls", full + [0]),
            ("invalid: extra after open last frame", [0] * (2 * frames) + [1]),
        ]
        for name, rolls in invalid:
            try:
                reference_score(rolls, *args)
            except ValueError:
                out.append({"name": name, "rolls": rolls, "expected": "ValueError"})
                continue
            raise AssertionError(f"invalid case {name!r} was accepted by the reference")
        return out

    def grader_source(self, v: Variant) -> str:
        return _GRADER.replace("__MODULE__", json.dumps(v.names["module"])).replace(
            "__FUNCTION__", json.dumps(v.names["function"]))


_GRADER = r'''
import importlib, json, signal, sys, traceback

MODULE, FUNCTION = __MODULE__, __FUNCTION__
LIMIT = 20


class _Timeout(BaseException):
    pass


def _alarm(*_):
    raise _Timeout()


def main():
    cases = json.load(open("/grader/cases.json"))
    result = {"passed": 0, "failed": 0, "errors": []}

    def fail(case, expected, got):
        result["failed"] += 1
        if len(result["errors"]) < LIMIT:
            result["errors"].append({"case": case["name"], "expected": expected, "got": str(got)[:300]})

    try:
        sys.path.insert(0, "/work")
        sys.dont_write_bytecode = True
        fn = getattr(importlib.import_module(MODULE), FUNCTION)
    except BaseException as exc:
        why = "import failed: " + "".join(traceback.format_exception_only(type(exc), exc)).strip()
        for c in cases:
            fail(c, c["expected"], why)
        return result
    try:
        signal.signal(signal.SIGALRM, _alarm)
    except Exception:
        pass
    for c in cases:
        try:
            signal.alarm(5)
            try:
                got = fn(list(c["rolls"]))
            finally:
                signal.alarm(0)
        except _Timeout:
            fail(c, c["expected"], "timeout after 5 s")
            continue
        except ValueError as exc:
            if c["expected"] == "ValueError":
                result["passed"] += 1
            else:
                fail(c, c["expected"], "ValueError: " + str(exc))
            continue
        except BaseException as exc:
            fail(c, c["expected"], type(exc).__name__ + ": " + str(exc))
            continue
        if c["expected"] != "ValueError" and type(got) is int and got == c["expected"]:
            result["passed"] += 1
        else:
            fail(c, c["expected"], "no ValueError, returned " + repr(got) if c["expected"] == "ValueError" else repr(got))
    return result


try:
    out = main()
except BaseException as exc:
    out = {"passed": 0, "failed": 0, "errors": [{"case": "grader", "expected": "", "got": repr(exc)}]}
print(json.dumps(out))
'''


def _self_check() -> None:
    """Run manually: python -c 'from maef_api.bench.families.bowling import _self_check; _self_check()'."""
    s = lambda r, *a: reference_score(r, *(a or (10, 10, 2, 1)))
    assert s([0] * 20) == 0
    assert s([1] * 20) == 20
    assert s([10] * 12) == 300
    assert s([5] * 21) == 150
    assert s([5, 5, 3] + [0] * 17) == 16
    assert s([10, 3, 4] + [0] * 16) == 24
    assert s([0] * 18 + [10, 7, 3]) == 20
    assert s([0] * 18 + [5, 5, 10]) == 20
    for bad in ([], [-1] + [0] * 19, [11] + [0] * 19, [5, 6] + [0] * 18, [0] * 19, [0] * 21, [10] * 11, [10] * 13):
        try:
            s(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    # generator yields only valid games for every variant
    fam = Bowling()
    for seed in range(50):
        for noise in range(3):
            v = fam.sample(seed, noise)
            cases = fam.hidden_cases(v)
            assert len(cases) >= 40 and sum(c["expected"] == "ValueError" for c in cases) == 8
            assert cases == fam.hidden_cases(v)
    print("bowling self-check ok")
