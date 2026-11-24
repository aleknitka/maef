# Benchmark report: bowling seed 9 noise 2

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `171e4e8567334255a4fb975b7489599f`
- Agent: `tdd-engineer` (repo revision `05ca86d`, definition `13e52fac41046cddca5218136b2ad55f5c2b4e078414b9af05ffdc84534c53c5`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: succeeded (completed)
- Wall time: 31.71 s
- Turns: 9; tool calls: 9 {'other_tool': 2, 'file_write': 5, 'test_run': 2}
- Tokens: input 46332, cached 36096, output 13249, reasoning 7292, total 59581
- Cost: $0.0104

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.8 | 737 | 1480 | 0 | 94 | 9 | 0.0001392 | 2 |
| 2 | 10.7 | 9849 | 2210 | 0 | 5826 | 4597 | 0.0038271 | 1 |
| 3 | 11.5 | 589 | 3476 | 2176 | 65 | 0 | 0.00029928 | 1 |
| 4 | 16.4 | 4217 | 3847 | 3456 | 2164 | 1051 | 0.00146073 | 1 |
| 5 | 21.0 | 4403 | 4997 | 3840 | 2086 | 828 | 0.00154035 | 1 |
| 6 | 23.3 | 2030 | 6292 | 4992 | 1245 | 143 | 0.00109176 | 1 |
| 7 | 25.1 | 1603 | 7431 | 6272 | 419 | 354 | 0.00061341 | 1 |
| 8 | 28.4 | 2584 | 8005 | 7424 | 854 | 302 | 0.00082227 | 1 |
| 9 | 30.3 | 1744 | 8594 | 7936 | 496 | 8 | 0.00063438 | 0 |

## TDD analysis

- **Verdict: test-first (red→green)**
- First test file written: turn 2; first implementation written: turn 4; tests written first: True
- First test run: turn 3, failed first (red): True
- Red to green cycles: 1; turns to first green: 7; final tests green: True
- Rewrites per file: /workspace/out/test_alley.py x0, /workspace/out/alley.py x2, /workspace/out/NOTES.md x0

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|
| 3 | False | 1 | 0 | 1 |
| 7 | True | 11 | 0 | 0 |

## Hidden acceptance

**45 / 45 passed** (0 failed)

## The agent's own tests

Last run at turn 7: passed (ran 11, failures 0, errors 0)

## Deliverables

- Present: alley.py, test_alley.py, NOTES.md
- Missing: none

## Variant versus canonical

| parameter | this variant | canonical |
|---|---|---|
| frames | 4 | 10 |
| pins | 5 | 10 |
| spare_bonus_rolls | 2 | 1 |
| strike_bonus_rolls | 3 | 2 |
| name: function | total | score |
| name: module | alley | bowling |
