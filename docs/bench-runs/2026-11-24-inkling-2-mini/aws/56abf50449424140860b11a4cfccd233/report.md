# Benchmark report: bowling seed 7 noise 2

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `56abf50449424140860b11a4cfccd233`
- Agent: `tdd-engineer` (repo revision `05ca86d`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 57.65 s
- Turns: 13; tool calls: 12 {'other_tool': 2, 'file_write': 8, 'test_run': 5, 'other_command': 1}
- Tokens: input 84955, cached 69632, output 17150, reasoning 10091, total 102105
- Cost: $0.0075

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.7 | 655 | 1484 | 0 | 53 | 9 | 0.0002544 | 1 |
| 2 | 1.4 | 587 | 1863 | 0 | 42 | 0 | 0.000152325 | 1 |
| 3 | 20.4 | 19000 | 2215 | 640 | 6799 | 4860 | 0.002167425 | 1 |
| 4 | 25.6 | 4885 | 4191 | 2176 | 1890 | 1306 | 0.000750765 | 1 |
| 5 | 27.6 | 1749 | 4816 | 4096 | 539 | 272 | 0.00027714 | 1 |
| 6 | 32.8 | 4480 | 5394 | 4736 | 1411 | 406 | 0.00054369 | 1 |
| 7 | 34.5 | 1429 | 6436 | 5376 | 209 | 144 | 0.00022284 | 1 |
| 8 | 39.5 | 4367 | 7034 | 6400 | 1725 | 837 | 0.00066105 | 1 |
| 9 | 44.4 | 4216 | 8391 | 6912 | 1707 | 939 | 0.000726705 | 1 |
| 10 | 47.9 | 2695 | 9722 | 8320 | 799 | 737 | 0.00046965 | 1 |
| 11 | 50.0 | 1912 | 10196 | 9600 | 810 | 310 | 0.0004317 | 1 |
| 12 | 54.5 | 3826 | 11375 | 10112 | 630 | 204 | 0.000435405 | 1 |
| 13 | 56.1 | 1353 | 11838 | 11264 | 536 | 67 | 0.00037281 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 3; first implementation written: turn 6; tests written first: True
- First test run: turn 5, failed first (red): True
- Red to green cycles: 1; turns to first green: 11; final tests green: True
- Rewrites per file: /workspace/out/test_alley.py x0, /workspace/test_alley.py x4, /workspace/out/alley.py x0, /workspace/out/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 5 | False | 1 | 0 | 1 |
| 7 | False | 15 | 0 | 2 |
| 8 | False | 15 | 0 | 2 |
| 9 | False | 15 | 0 | 1 |
| 11 | True | 15 | 0 | 0 |

## Hidden acceptance

**44 / 45 passed** (1 failed)
- `random game 20`: expected 59, got ValueError: roll exceeds pins standing

## The agent's own tests

Last run at turn 11: passed (ran 15, failures 0, errors 0)

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
