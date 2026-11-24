# Benchmark report: bowling seed 3 noise 0

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `fd17150ddd344279ad43a0cacdcb1bc5`
- Agent: `tdd-engineer` (repo revision `05ca86d`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 40.88 s
- Turns: 11; tool calls: 10 {'other_tool': 2, 'file_write': 6, 'test_run': 5}
- Tokens: input 54328, cached 47232, output 15751, reasoning 10602, total 70079
- Cost: $0.0119

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.6 | 580 | 1483 | 1408 | 53 | 9 | 8.529e-05 | 1 |
| 2 | 1.2 | 502 | 1862 | 1408 | 42 | 0 | 0.00013554 | 1 |
| 3 | 13.6 | 12372 | 2214 | 2176 | 6386 | 5006 | 0.00390258 | 1 |
| 4 | 14.4 | 627 | 3631 | 2176 | 81 | 16 | 0.00033213 | 1 |
| 5 | 22.5 | 7344 | 4007 | 3584 | 3939 | 2600 | 0.00253437 | 1 |
| 6 | 24.0 | 1292 | 5383 | 3968 | 386 | 321 | 0.00056289 | 1 |
| 7 | 26.4 | 1793 | 6133 | 5376 | 834 | 462 | 0.00077523 | 1 |
| 8 | 29.7 | 2411 | 6682 | 6016 | 966 | 708 | 0.00085998 | 1 |
| 9 | 32.2 | 1817 | 7026 | 6656 | 807 | 450 | 0.00073938 | 1 |
| 10 | 36.1 | 3189 | 7564 | 6912 | 1092 | 350 | 0.00096036 | 1 |
| 11 | 39.2 | 2822 | 8343 | 7552 | 1165 | 680 | 0.00104421 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 3; first implementation written: turn 5; tests written first: True
- First test run: turn 4, failed first (red): True
- Red to green cycles: 2; turns to first green: 7; final tests green: True
- Rewrites per file: /workspace/out/test_bowling.py x0, /workspace/out/bowling.py x0, /workspace/test_bowling.py x2, /workspace/out/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 4 | False | 1 | 0 | 1 |
| 6 | False | 14 | 1 | 0 |
| 7 | True | 14 | 0 | 0 |
| 8 | False | 15 | 0 | 1 |
| 9 | True | 15 | 0 | 0 |

## Hidden acceptance

**45 / 45 passed** (0 failed)

## The agent's own tests

Last run at turn 9: passed (ran 15, failures 0, errors 0)

## Deliverables

- Present: bowling.py, test_bowling.py, NOTES.md
- Missing: none

## Variant versus canonical

| parameter | this variant | canonical |
|---|---|---|
| frames | 10 | 10 |
| pins | 10 | 10 |
| spare_bonus_rolls | 1 | 1 |
| strike_bonus_rolls | 2 | 2 |
| name: function | score | score |
| name: module | bowling | bowling |
