# Benchmark report: bowling seed 4 noise 1

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `815cbfd2adb14cb2bc62b2fb9a82ac19`
- Agent: `tdd-engineer` (repo revision `05ca86d`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 37.16 s
- Turns: 8; tool calls: 7 {'other_tool': 2, 'file_write': 3, 'test_run': 2}
- Tokens: input 29973, cached 21888, output 15696, reasoning 12229, total 45669
- Cost: $0.0112

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 1.1 | 1074 | 1483 | 0 | 53 | 9 | 0.000127125 | 1 |
| 2 | 1.9 | 739 | 1862 | 0 | 51 | 9 | 0.0003099 | 1 |
| 3 | 21.7 | 19687 | 2214 | 1792 | 10584 | 9358 | 0.00646746 | 1 |
| 4 | 22.5 | 643 | 3477 | 2176 | 65 | 0 | 0.00029943 | 1 |
| 5 | 29.5 | 6298 | 3854 | 3456 | 3295 | 2106 | 0.00214038 | 1 |
| 6 | 31.2 | 1519 | 5080 | 3840 | 411 | 346 | 0.0005478 | 1 |
| 7 | 34.1 | 2206 | 5753 | 4992 | 688 | 228 | 0.00067671 | 1 |
| 8 | 35.8 | 1576 | 6250 | 5632 | 549 | 173 | 0.00059106 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 3; first implementation written: turn 5; tests written first: True
- First test run: turn 4, failed first (red): True
- Red to green cycles: 1; turns to first green: 6; final tests green: True
- Rewrites per file: /workspace/out/test_tenpin.py x0, /workspace/out/tenpin.py x0, /workspace/out/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 4 | False | 1 | 0 | 1 |
| 6 | True | 14 | 0 | 0 |

## Hidden acceptance

**45 / 45 passed** (0 failed)

## The agent's own tests

Last run at turn 6: passed (ran 14, failures 0, errors 0)

## Deliverables

- Present: tenpin.py, test_tenpin.py, NOTES.md
- Missing: none

## Variant versus canonical

| parameter | this variant | canonical |
|---|---|---|
| frames | 12 | 10 |
| pins | 13 | 10 |
| spare_bonus_rolls | 1 | 1 |
| strike_bonus_rolls | 2 | 2 |
| name: function | tally | score |
| name: module | tenpin | bowling |
