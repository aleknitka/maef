# Benchmark report: bowling seed 3 noise 1

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `be236aa14eaf4d0aa767175efa4ee3be`
- Agent: `tdd-engineer` (repo revision `05ca86d`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 45.57 s
- Turns: 10; tool calls: 9 {'other_tool': 2, 'file_write': 5, 'test_run': 3}
- Tokens: input 43735, cached 34688, output 12478, reasoning 7985, total 56213
- Cost: $0.0098

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.9 | 901 | 1484 | 0 | 54 | 10 | 0.0001275 | 1 |
| 2 | 1.6 | 691 | 1863 | 0 | 42 | 0 | 0.00030465 | 1 |
| 3 | 13.8 | 12221 | 2215 | 1792 | 6334 | 5063 | 0.00391761 | 1 |
| 4 | 15.5 | 688 | 3523 | 2176 | 65 | 0 | 0.00030633 | 1 |
| 5 | 21.6 | 4819 | 3917 | 3584 | 2418 | 1712 | 0.00160827 | 1 |
| 6 | 24.6 | 2091 | 4660 | 3840 | 1105 | 33 | 0.0009012 | 1 |
| 7 | 27.5 | 1993 | 5769 | 4608 | 676 | 611 | 0.00071799 | 1 |
| 8 | 30.2 | 1487 | 6350 | 5760 | 565 | 277 | 0.0006003 | 1 |
| 9 | 33.8 | 2235 | 6709 | 6272 | 770 | 271 | 0.00071571 | 1 |
| 10 | 36.0 | 1261 | 7245 | 6656 | 449 | 8 | 0.00055743 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 3; first implementation written: turn 5; tests written first: True
- First test run: turn 4, failed first (red): True
- Red to green cycles: 1; turns to first green: 7; final tests green: True
- Rewrites per file: /workspace/out/test_alley.py x0, /workspace/out/alley.py x1, /workspace/alley.py x0, /workspace/out/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 4 | False | 1 | 0 | 1 |
| 7 | True | 13 | 0 | 0 |
| 8 | True | 13 | 0 | 0 |

## Hidden acceptance

**45 / 45 passed** (0 failed)

## The agent's own tests

Last run at turn 8: passed (ran 13, failures 0, errors 0)

## Deliverables

- Present: alley.py, test_alley.py, NOTES.md
- Missing: none

## Variant versus canonical

| parameter | this variant | canonical |
|---|---|---|
| frames | 7 | 10 |
| pins | 11 | 10 |
| spare_bonus_rolls | 1 | 1 |
| strike_bonus_rolls | 2 | 2 |
| name: function | final_score | score |
| name: module | alley | bowling |
