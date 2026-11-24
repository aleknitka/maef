# Benchmark report: bowling seed 1 noise 0

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `b46dc0babff44494934ec73b7106bba1`
- Agent: `tdd-engineer` (repo revision `05ca86d`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 34.12 s
- Turns: 9; tool calls: 8 {'other_tool': 2, 'file_write': 5, 'test_run': 3}
- Tokens: input 39628, cached 31104, output 13514, reasoning 8290, total 53142
- Cost: $0.0103

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.8 | 748 | 1483 | 0 | 54 | 10 | 0.00025485 | 1 |
| 2 | 1.4 | 531 | 1862 | 1408 | 42 | 0 | 0.00013554 | 1 |
| 3 | 9.7 | 8259 | 2214 | 1792 | 3806 | 2109 | 0.00240066 | 1 |
| 4 | 11.2 | 1215 | 3948 | 2176 | 367 | 302 | 0.00055128 | 1 |
| 5 | 20.2 | 8423 | 4324 | 3840 | 4349 | 3737 | 0.0027972 | 1 |
| 6 | 22.3 | 1675 | 4977 | 4224 | 724 | 172 | 0.00067407 | 1 |
| 7 | 28.0 | 5470 | 5570 | 4864 | 3156 | 1825 | 0.00214542 | 1 |
| 8 | 30.3 | 1663 | 7371 | 5504 | 580 | 127 | 0.00079317 | 1 |
| 9 | 32.2 | 1260 | 7879 | 7296 | 436 | 8 | 0.00056793 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 3; first implementation written: turn 6; tests written first: True
- First test run: turn 4, failed first (red): True
- Red to green cycles: 1; turns to first green: 7; final tests green: True
- Rewrites per file: /workspace/out/test_bowling.py x0, /workspace/test_bowling.py x0, /workspace/out/bowling.py x1, /workspace/out/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 4 | False | 1 | 0 | 1 |
| 7 | True | 12 | 0 | 0 |
| 8 | True | 12 | 0 | 0 |

## Hidden acceptance

**45 / 45 passed** (0 failed)

## The agent's own tests

Last run at turn 8: passed (ran 12, failures 0, errors 0)

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
