# Benchmark report: bowling seed 2 noise 0

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `b6cce13f4dfd489e8d6272eba4712091`
- Agent: `tdd-engineer` (repo revision `05ca86d`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 28.05 s
- Turns: 12; tool calls: 11 {'other_tool': 2, 'file_write': 7, 'test_run': 8}
- Tokens: input 60781, cached 48768, output 8402, reasoning 3326, total 69183
- Cost: $0.0082

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.7 | 628 | 1483 | 0 | 54 | 10 | 0.000127425 | 1 |
| 2 | 1.3 | 586 | 1862 | 1408 | 42 | 0 | 0.00013554 | 1 |
| 3 | 2.2 | 823 | 2214 | 2176 | 244 | 11 | 0.00021738 | 1 |
| 4 | 3.0 | 565 | 2484 | 2176 | 61 | 0 | 0.00014808 | 1 |
| 5 | 8.8 | 5149 | 2879 | 2432 | 2493 | 1509 | 0.00163581 | 1 |
| 6 | 11.7 | 2262 | 4189 | 2816 | 1239 | 387 | 0.00103383 | 1 |
| 7 | 13.6 | 1174 | 5375 | 4096 | 379 | 210 | 0.00054213 | 1 |
| 8 | 17.0 | 2657 | 5666 | 5248 | 1316 | 67 | 0.00100974 | 1 |
| 9 | 18.2 | 605 | 7321 | 5376 | 65 | 0 | 0.00049203 | 1 |
| 10 | 21.5 | 2609 | 8264 | 5632 | 1429 | 859 | 0.00142116 | 1 |
| 11 | 24.6 | 2316 | 9270 | 8192 | 722 | 273 | 0.00084066 | 1 |
| 12 | 26.3 | 1095 | 9774 | 9216 | 358 | 0 | 0.00057498 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 3; first implementation written: turn 5; tests written first: True
- First test run: turn 4, failed first (red): True
- Red to green cycles: 2; turns to first green: 7; final tests green: True
- Rewrites per file: /workspace/out/test_bowling.py x0, /workspace/bowling.py x1, /workspace/test_bowling.py x2, /workspace/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 4 | False | 1 | 0 | 1 |
| 5 | False | 2 | 0 | 1 |
| 6 | False | 2 | 0 | 1 |
| 7 | True | 2 | 0 | 0 |
| 8 | False | 11 | 2 | 1 |
| 9 | True | 0 | 0 | 0 |
| 10 | True | 11 | 0 | 0 |
| 11 | True | 11 | 0 | 0 |

## Hidden acceptance

**45 / 45 passed** (0 failed)

## The agent's own tests

Last run at turn 11: passed (ran 11, failures 0, errors 0)

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
