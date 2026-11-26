from maef_api.guard import BudgetGuard
from maef_api.models import Budget


def make(clock=lambda: 0.0, reserved=10_000, max_output=100, **budget):
    return BudgetGuard(Budget(**budget), reserved_tokens=reserved, max_output_tokens=max_output, clock=clock)


def test_first_turn_allowed_within_budget():
    assert make(max_turns=5).check(prompt_bytes=10) is None


def test_turns_exhausted_after_max_turns_recorded():
    guard = make(max_turns=2)
    guard.record(1, 1)
    assert guard.check(10) is None
    guard.record(1, 1)
    assert guard.check(10) == "turns_exhausted"


def test_tokens_exhausted_at_reservation_boundary():
    # tokens_used + prompt_bytes + max_output_tokens must fit in the reservation.
    guard = make(reserved=100, max_output=10, max_turns=5)
    guard.record(20, 20)  # 40 used
    assert guard.check(prompt_bytes=50) is None  # 40 + 50 + 10 == 100
    assert guard.check(prompt_bytes=51) == "tokens_exhausted"  # 101 > 100


def test_duration_exhausted_with_fake_clock():
    now = [0.0]
    guard = make(clock=lambda: now[0], max_duration_seconds=10, max_turns=5)
    now[0] = 9.0
    assert guard.check(10) is None
    now[0] = 10.0
    assert guard.check(10) == "duration_exhausted"


def test_remaining_seconds_decreases_with_clock():
    now = [100.0]
    guard = make(clock=lambda: now[0], max_duration_seconds=60)
    assert guard.remaining_seconds() == 60.0
    now[0] = 130.0
    assert guard.remaining_seconds() == 30.0


def test_tokens_and_turns_accumulate():
    guard = make(max_turns=5)
    assert (guard.tokens_used, guard.turns) == (0, 0)
    guard.record(3, 4)
    guard.record(5, 6)
    assert (guard.tokens_used, guard.turns) == (18, 2)
