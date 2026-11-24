# Benchmark report: bowling seed 5 noise 1

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `c1221f008dd842e396f416b53b96c8dd`
- Agent: `tdd-engineer` (repo revision `05ca86d`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 75.89 s
- Turns: 10; tool calls: 10 {'other_tool': 2, 'file_write': 5, 'test_run': 4}
- Tokens: input 58329, cached 45696, output 12894, reasoning 6389, total 71223
- Cost: $0.0056

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.8 | 735 | 1490 | 0 | 95 | 10 | 0.0002805 | 2 |
| 2 | 16.7 | 15861 | 2220 | 0 | 3652 | 2396 | 0.0012621 | 1 |
| 3 | 17.6 | 710 | 3513 | 2176 | 65 | 0 | 0.000152415 | 1 |
| 4 | 37.9 | 19583 | 3923 | 3456 | 2976 | 1818 | 0.000979665 | 1 |
| 5 | 43.0 | 4917 | 5118 | 3840 | 1399 | 185 | 0.00057315 | 1 |
| 6 | 44.4 | 1236 | 6369 | 4992 | 149 | 84 | 0.000222855 | 1 |
| 7 | 49.5 | 4396 | 6976 | 6272 | 907 | 56 | 0.00041898 | 1 |
| 8 | 60.3 | 10168 | 8291 | 6912 | 2542 | 1660 | 0.000969705 | 1 |
| 9 | 72.2 | 10783 | 9949 | 8192 | 653 | 159 | 0.000450555 | 1 |
| 10 | 74.5 | 2166 | 10480 | 9856 | 456 | 21 | 0.00033144 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 2; first implementation written: turn 4; tests written first: True
- First test run: turn 3, failed first (red): True
- Red to green cycles: 2; turns to first green: 6; final tests green: True
- Rewrites per file: /workspace/out/test_skittles.py x1, /workspace/out/skittles.py x1, /workspace/out/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 3 | False | 1 | 0 | 1 |
| 6 | True | 12 | 0 | 0 |
| 7 | False | 18 | 0 | 2 |
| 8 | True | 17 | 0 | 0 |

## Hidden acceptance

**45 / 45 passed** (0 failed)

## The agent's own tests

Last run at turn 8: passed (ran 17, failures 0, errors 0)

## Deliverables

- Present: skittles.py, test_skittles.py, NOTES.md
- Missing: none

## Variant versus canonical

| parameter | this variant | canonical |
|---|---|---|
| frames | 6 | 10 |
| pins | 12 | 10 |
| spare_bonus_rolls | 1 | 1 |
| strike_bonus_rolls | 2 | 2 |
| name: function | final_score | score |
| name: module | skittles | bowling |
