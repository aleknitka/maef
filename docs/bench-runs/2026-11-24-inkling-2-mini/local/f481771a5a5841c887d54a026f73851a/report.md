# Benchmark report: bowling seed 7 noise 2

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `f481771a5a5841c887d54a026f73851a`
- Agent: `tdd-engineer` (repo revision `8ba7890`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 76.73 s
- Turns: 10; tool calls: 9 {'other_tool': 2, 'other_command': 1, 'file_write': 4, 'test_run': 3}
- Tokens: input 39320, cached 34048, output 17940, reasoning 14003, total 57260
- Cost: $0.0063

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.9 | 927 | 1484 | 1408 | 54 | 10 | 4.302e-05 | 1 |
| 2 | 1.9 | 915 | 1863 | 1408 | 42 | 0 | 6.7845e-05 | 1 |
| 3 | 25.0 | 23128 | 2215 | 2176 | 6968 | 6922 | 0.002125965 | 1 |
| 4 | 39.8 | 13857 | 2298 | 2176 | 4774 | 3526 | 0.00147399 | 1 |
| 5 | 41.6 | 707 | 3583 | 2176 | 61 | 0 | 0.000156465 | 1 |
| 6 | 52.6 | 9629 | 3973 | 3584 | 3346 | 2192 | 0.001086735 | 1 |
| 7 | 56.2 | 2687 | 5164 | 3968 | 668 | 607 | 0.00034962 | 1 |
| 8 | 61.1 | 3644 | 5777 | 5120 | 880 | 418 | 0.000390075 | 1 |
| 9 | 64.8 | 2767 | 6276 | 5760 | 552 | 209 | 0.0002907 | 1 |
| 10 | 68.0 | 1833 | 6687 | 6272 | 595 | 119 | 0.000303705 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 4; first implementation written: turn 6; tests written first: True
- First test run: turn 5, failed first (red): True
- Red to green cycles: 1; turns to first green: 7; final tests green: True
- Rewrites per file: /workspace/out/test_alley.py x0, /workspace/out/alley.py x1, /workspace/out/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 5 | False | 1 | 0 | 1 |
| 7 | True | 13 | 0 | 0 |
| 9 | True | 13 | 0 | 0 |

## Hidden acceptance

**45 / 45 passed** (0 failed)

## The agent's own tests

Last run at turn 9: passed (ran 13, failures 0, errors 0)

## Deliverables

- Present: alley.py, test_alley.py, NOTES.md
- Missing: none

## Variant versus canonical

| parameter | this variant | canonical |
|---|---|---|
| frames | 6 | 10 |
| pins | 6 | 10 |
| spare_bonus_rolls | 2 | 1 |
| strike_bonus_rolls | 3 | 2 |
| name: function | final_score | score |
| name: module | alley | bowling |
