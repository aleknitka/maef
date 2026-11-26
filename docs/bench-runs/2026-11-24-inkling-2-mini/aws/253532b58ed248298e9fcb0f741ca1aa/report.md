# Benchmark report: bowling seed 3 noise 1

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `253532b58ed248298e9fcb0f741ca1aa`
- Agent: `tdd-engineer` (repo revision `05ca86d`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 34.22 s
- Turns: 8; tool calls: 7 {'other_tool': 2, 'file_write': 4, 'test_run': 2}
- Tokens: input 32141, cached 23168, output 12121, reasoning 7940, total 44262
- Cost: $0.0092

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 1.0 | 921 | 1484 | 0 | 54 | 10 | 0.0001275 | 1 |
| 2 | 1.6 | 603 | 1863 | 0 | 51 | 9 | 0.00031005 | 1 |
| 3 | 17.9 | 16170 | 2215 | 1792 | 7716 | 6288 | 0.00474681 | 1 |
| 4 | 18.7 | 649 | 3680 | 2176 | 61 | 0 | 0.00032748 | 1 |
| 5 | 24.2 | 4790 | 4070 | 3584 | 1917 | 680 | 0.00133062 | 1 |
| 6 | 25.6 | 1226 | 5344 | 3968 | 478 | 63 | 0.00061224 | 1 |
| 7 | 30.1 | 3725 | 6439 | 5248 | 1140 | 570 | 0.00102009 | 1 |
| 8 | 32.4 | 2094 | 7046 | 6400 | 704 | 320 | 0.0007113 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 3; first implementation written: turn 5; tests written first: True
- First test run: turn 4, failed first (red): True
- Red to green cycles: 1; turns to first green: 6; final tests green: True
- Rewrites per file: /workspace/out/test_alley.py x0, /workspace/out/alley.py x1, /workspace/out/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 4 | False | 1 | 0 | 1 |
| 6 | True | 16 | 0 | 0 |

## Hidden acceptance

**45 / 45 passed** (0 failed)

## The agent's own tests

Last run at turn 6: passed (ran 16, failures 0, errors 0)

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
