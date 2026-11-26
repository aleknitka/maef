# Benchmark report: bowling seed 8 noise 2

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `20273cb5fd1e41328758bd84e1cdc45b`
- Agent: `tdd-engineer` (repo revision `05ca86d`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 97.17 s
- Turns: 15; tool calls: 14 {'other_tool': 2, 'file_write': 11, 'test_run': 10}
- Tokens: input 82661, cached 68992, output 23911, reasoning 17647, total 106572
- Cost: $0.0092

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 6.2 | 6117 | 1486 | 0 | 53 | 9 | 0.00012735 | 1 |
| 2 | 6.8 | 560 | 1865 | 640 | 42 | 0 | 0.000114075 | 1 |
| 3 | 31.3 | 24429 | 2217 | 1792 | 6852 | 6602 | 0.002114355 | 1 |
| 4 | 32.5 | 991 | 2504 | 2176 | 132 | 71 | 9.684e-05 | 1 |
| 5 | 48.5 | 15311 | 2908 | 2432 | 5932 | 5837 | 0.00185178 | 1 |
| 6 | 63.6 | 14500 | 3270 | 2816 | 3735 | 2634 | 0.00119679 | 1 |
| 7 | 66.2 | 1964 | 4711 | 3200 | 436 | 217 | 0.000292125 | 1 |
| 8 | 68.7 | 1829 | 5029 | 4608 | 430 | 148 | 0.000229695 | 1 |
| 9 | 70.6 | 1700 | 5352 | 4992 | 595 | 43 | 0.00028038 | 1 |
| 10 | 73.6 | 2285 | 6065 | 5248 | 1024 | 229 | 0.000447195 | 1 |
| 11 | 76.0 | 1803 | 7494 | 6016 | 475 | 162 | 0.00034359 | 1 |
| 12 | 82.0 | 5246 | 8178 | 7424 | 1108 | 761 | 0.00050031 | 1 |
| 13 | 88.8 | 6100 | 9048 | 8064 | 1884 | 727 | 0.00075996 | 1 |
| 14 | 93.2 | 3590 | 10728 | 8960 | 755 | 200 | 0.0004935 | 1 |
| 15 | 95.4 | 1560 | 11806 | 10624 | 458 | 7 | 0.00038541 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 3; first implementation written: turn 5; tests written first: True
- First test run: turn 4, failed first (red): True
- Red to green cycles: 2; turns to first green: 7; final tests green: True
- Rewrites per file: /workspace/out/test_skittles.py x0, /workspace/skittles.py x2, /workspace/test_skittles.py x5, /workspace/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 4 | False | 1 | 0 | 1 |
| 5 | False | 1 | 0 | 1 |
| 6 | False | 1 | 0 | 1 |
| 7 | True | 1 | 0 | 0 |
| 9 | True | 3 | 0 | 0 |
| 10 | False | 8 | 0 | 1 |
| 11 | True | 8 | 0 | 0 |
| 12 | True | 13 | 0 | 0 |
| 13 | True | 13 | 0 | 0 |
| 14 | True | 13 | 0 | 0 |

## Hidden acceptance

**45 / 45 passed** (0 failed)

## The agent's own tests

Last run at turn 14: passed (ran 13, failures 0, errors 0)

## Deliverables

- Present: skittles.py, test_skittles.py, NOTES.md
- Missing: none

## Variant versus canonical

| parameter | this variant | canonical |
|---|---|---|
| frames | 8 | 10 |
| pins | 5 | 10 |
| spare_bonus_rolls | 2 | 1 |
| strike_bonus_rolls | 1 | 2 |
| name: function | total | score |
| name: module | skittles | bowling |
