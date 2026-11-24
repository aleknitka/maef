# Task: score a game of bowling

## Context

You are scoring a bowling game with slightly unusual rules. Read them carefully: they may differ from the
bowling you know. A game has **6 frames**. Every frame starts with **6 pins** standing. You have no
network and no pytest; only Python 3 and its standard library are available in your sandbox.

## Requirements

R1. Write a function `final_score(rolls: list[int]) -> int` in a module named `alley`. `rolls` is the list of pins knocked
    down by each ball, in order, for exactly one complete game. It returns the total score.
R2. Frames 1 to 5: a player gets up to two balls per frame. Knocking down all 6 pins with the first
    ball is a **strike** and the frame ends. Knocking down all 6 pins with two balls is a **spare**. Anything
    else is an **open** frame.
R3. An open frame scores the pins knocked down in it.
R4. A strike scores 6 plus the pins knocked down by the next **3** ball(s) (the following frames' balls,
    counted in order).
R5. A spare scores 6 plus the pins knocked down by the next **2** ball(s).
R6. If fewer balls were thrown after a frame than its bonus asks for, only the balls that exist count.
R7. Frame 6 (the last): the player throws a first ball. After a strike the pins reset and the player gets
    **3** extra "fill" ball(s). Otherwise the player throws a second ball; if that clears all pins (a spare) the
    pins reset and the player gets **2** fill ball(s); otherwise the frame ends with no fill balls.
    In the last frame, pins are reset to 6 whenever all pins are down (after a strike, or after a fill ball
    that clears the pins standing). A fill ball may not knock down more pins than are standing.
R8. The last frame scores just the sum of its own balls, with no bonus on top. Its balls can still serve as bonus
    balls for earlier frames.
R9. Raise `ValueError` for invalid input: a negative roll, a roll larger than the pins standing at that moment,
    too few rolls (the game is not finished), or too many rolls (extra rolls after the game is over). A valid
    `rolls` list is exactly one complete game, nothing more or less. An empty list is invalid.

## Deliverables

D1. `/workspace/out/alley.py` containing `final_score`.
D2. `/workspace/out/test_alley.py`: your own tests, using the standard library `unittest` module (pytest is NOT
    available).
D3. `/workspace/out/NOTES.md`: your approach, your assumptions, and how to run the tests:
    `cd /workspace/out && python3 -m unittest -v`.

## How this will be judged

Your work is judged by hidden acceptance tests that call your public API exactly as named above. Your own tests also count: they should be real, runnable and cover the rules.
