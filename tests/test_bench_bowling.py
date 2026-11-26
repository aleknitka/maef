"""Bowling task family: reference rules, variant sampling, hidden cases, task text, grader source and the
`maef bench variant` CLI."""

import json

import pytest
from maef_api.bench.families import FAMILIES
from maef_api.bench.families.base import Variant
from maef_api.bench.families.bowling import (
    CANONICAL,
    CANONICAL_NAMES,
    Bowling,
    reference_score,
)
from maef_api.cli import main

FAM = Bowling()


def score(rolls, pins=10, frames=10, sb=2, pb=1):
    return reference_score(rolls, pins, frames, sb, pb)


def variant(params=None, names=None, seed=1, noise=1):
    return Variant(
        family="bowling", seed=seed, noise=noise, params=params or dict(CANONICAL), names=names or dict(CANONICAL_NAMES)
    )


# --- canonical scoring -------------------------------------------------------------------------


def test_canonical_known_totals():
    assert score([0] * 20) == 0  # gutter game
    assert score([1] * 20) == 20  # all ones
    assert score([10] * 12) == 300  # perfect game
    assert score([5] * 21) == 150  # all spares, fill ball of 5


def test_canonical_hand_checked_frames():
    assert score([5, 5, 3] + [0] * 17) == 16  # spare + 3 bonus
    assert score([10, 3, 4] + [0] * 16) == 24  # strike + 3 + 4 bonus
    assert score([0] * 18 + [10, 7, 3]) == 20  # last-frame strike: own balls only, no bonus on top
    assert score([0] * 18 + [5, 5, 10]) == 20  # last-frame spare: fill ball resets pins


def test_last_frame_fill_balls_reset_pins():
    assert score([0] * 18 + [10, 10, 10]) == 30  # two fill balls, each on freshly reset pins
    assert score([0] * 18 + [10, 3, 4]) == 17


def test_non_canonical_rules_hand_checked():
    assert score([1] * 8, pins=5, frames=4, sb=3, pb=2) == 8
    assert score([5] * 7, pins=5, frames=4, sb=3, pb=2) == 80  # 3 strikes x (5 + 15 bonus) + last frame 4 balls x 5


@pytest.mark.parametrize(
    "bad",
    [
        [],  # empty game
        None,
        "0000",
        [-1] + [0] * 19,  # negative roll
        [11] + [0] * 19,  # above pins
        [1.5] + [0] * 19,  # not an int
        [True] + [0] * 19,  # bool is not an int here
        [5, 6] + [0] * 18,  # open frame over pins standing
        [0] * 19,  # too few rolls
        [0] * 21,  # too many rolls
        [10] * 11,  # perfect game without its fill balls
        [10] * 13,  # extra rolls after the game is over
    ],
)
def test_invalid_inputs_raise_value_error(bad):
    with pytest.raises(ValueError):
        score(bad)


# --- variant sampling ----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [0, 1, 7, 12345])
def test_noise_zero_is_canonical(seed):
    v = FAM.sample(seed, 0)
    assert v.params == CANONICAL
    assert v.names == {"module": "bowling", "function": "score"}
    assert FAM.deliverables(v) == ["bowling.py", "test_bowling.py", "NOTES.md"]


def test_noise_zero_canonical_task_and_cases():
    cases = {c["name"]: c for c in FAM.hidden_cases(FAM.sample(3, 0))}
    assert cases["gutter game"]["expected"] == 0
    assert cases["perfect game"]["expected"] == 300
    assert cases["all spares"]["expected"] == 150
    assert "`score(rolls: list[int]) -> int`" in FAM.render_task(FAM.sample(3, 0))


def test_sampling_is_deterministic():
    for seed, noise in [(0, 0), (5, 1), (5, 2), (99, 2)]:
        assert FAM.sample(seed, noise) == FAM.sample(seed, noise)
        assert FAM.hidden_cases(FAM.sample(seed, noise)) == FAM.hidden_cases(FAM.sample(seed, noise))
        assert FAM.render_task(FAM.sample(seed, noise)) == FAM.render_task(FAM.sample(seed, noise))


@pytest.mark.parametrize("noise", [1, 2])
def test_different_seeds_produce_different_variants(noise):
    variants = {json.dumps(FAM.sample(s, noise).model_dump(), sort_keys=True) for s in range(20)}
    assert len(variants) > 10
    assert FAM.hidden_cases(FAM.sample(1, noise)) != FAM.hidden_cases(FAM.sample(2, noise))
    assert FAM.render_task(FAM.sample(1, noise)) != FAM.render_task(FAM.sample(2, noise))


def test_noise_one_changes_numbers_and_names_within_range():
    for seed in range(30):
        v = FAM.sample(seed, 1)
        assert 5 <= v.params["pins"] <= 15
        assert 4 <= v.params["frames"] <= 12
        assert v.params["strike_bonus_rolls"] == CANONICAL["strike_bonus_rolls"]
        assert v.names["module"] in ("skittles", "tenpin", "lanes", "alley", "pinfall")
        assert v.names["function"] in ("total", "tally", "final_score", "game_score")


def test_noise_two_also_changes_bonus_lengths():
    for seed in range(30):
        v = FAM.sample(seed, 2)
        assert 1 <= v.params["strike_bonus_rolls"] <= 3
        assert 1 <= v.params["spare_bonus_rolls"] <= 2


# --- hidden cases ----------------------------------------------------------------------------------


@pytest.mark.parametrize("noise", [0, 1, 2])
def test_every_hidden_case_is_valid_for_the_reference(noise):
    for seed in range(40):
        v = FAM.sample(seed, noise)
        p = v.params
        cases = FAM.hidden_cases(v)  # raises AssertionError if the generator built an invalid hand case
        assert len(cases) >= 40
        assert sum(c["expected"] == "ValueError" for c in cases) == 8
        for c in cases:
            if c["expected"] == "ValueError":
                with pytest.raises(ValueError):
                    reference_score(c["rolls"], p["pins"], p["frames"], p["strike_bonus_rolls"], p["spare_bonus_rolls"])
            else:
                assert type(c["expected"]) is int
                assert (
                    reference_score(c["rolls"], p["pins"], p["frames"], p["strike_bonus_rolls"], p["spare_bonus_rolls"])
                    == c["expected"]
                )
                assert all(type(r) is int and 0 <= r <= p["pins"] for r in c["rolls"])


def test_hidden_case_names_are_unique():
    names = [c["name"] for c in FAM.hidden_cases(FAM.sample(4, 2))]
    assert len(names) == len(set(names))


# --- task text -------------------------------------------------------------------------------------


def test_render_task_numbers_requirements_and_deliverables():
    text = FAM.render_task(variant())
    for n in range(1, 10):
        assert f"R{n}. " in text or f"R{n} " in text
    for label in ("R1.", "R9.", "D1.", "D2.", "D3."):
        assert label in text
    assert "## Deliverables" in text and "## How this will be judged" in text


def test_render_task_contains_variant_names_and_params():
    v = variant(
        params={"pins": 7, "frames": 5, "strike_bonus_rolls": 3, "spare_bonus_rolls": 2},
        names={"module": "lanes", "function": "tally"},
    )
    text = FAM.render_task(v)
    assert "`tally(rolls: list[int]) -> int`" in text
    assert "module named `lanes`" in text
    assert "`/workspace/out/lanes.py`" in text and "`/workspace/out/test_lanes.py`" in text
    assert "**5 frames**" in text and "**7 pins**" in text
    assert "Frames 1 to 4" in text
    assert "**3** ball(s)" in text and "**2** ball(s)" in text


def test_render_task_names_come_from_the_variant_not_the_canonical_defaults():
    text = FAM.render_task(FAM.sample(11, 1))
    v = FAM.sample(11, 1)
    assert f"`{v.names['function']}(rolls" in text
    assert f"module named `{v.names['module']}`" in text


# --- grader source ---------------------------------------------------------------------------------


@pytest.mark.parametrize("noise", [0, 2])
def test_grader_source_is_valid_python_and_names_the_module(noise):
    v = FAM.sample(6, noise)
    src = FAM.grader_source(v)
    compile(src, "grade.py", "exec")
    assert f'MODULE, FUNCTION = "{v.names["module"]}", "{v.names["function"]}"' in src
    assert "__MODULE__" not in src and "__FUNCTION__" not in src
    assert "/grader/cases.json" in src


def test_family_is_registered():
    assert FAMILIES["bowling"].id == "bowling"
    assert isinstance(FAMILIES["bowling"], Bowling)


# --- maef bench variant (CLI) ----------------------------------------------------------------------


def test_cli_variant_prints_task_markdown(capsys):
    assert main(["bench", "variant", "--family", "bowling", "--seed", "7", "--noise", "2"]) == 0
    out = capsys.readouterr().out
    assert out == FAM.render_task(FAM.sample(7, 2)) + "\n"
    assert out.startswith("# Task: score a game of bowling")


def test_cli_variant_defaults_to_canonical(capsys):
    assert main(["bench", "variant"]) == 0
    assert capsys.readouterr().out.strip() == FAM.render_task(FAM.sample(0, 0)).strip()


def test_cli_variant_json_is_parseable(capsys):
    assert main(["bench", "variant", "--family", "bowling", "--seed", "7", "--noise", "2", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert Variant(**data["variant"]) == FAM.sample(7, 2)
    assert data["cases"] == FAM.hidden_cases(FAM.sample(7, 2))


def test_cli_variant_unknown_family_exits_2(capsys):
    assert main(["bench", "variant", "--family", "nope"]) == 2
    assert "unknown family" in capsys.readouterr().err
