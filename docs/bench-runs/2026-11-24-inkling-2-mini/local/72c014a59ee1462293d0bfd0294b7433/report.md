# Benchmark report: bowling seed 7 noise 2

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

- Run: `72c014a59ee1462293d0bfd0294b7433`
- Agent: `tdd-engineer` (repo revision `8ba7890`, definition `d0b3113f00e745455975444bb4bf7c51cae7f3abd285acf35f99c4e70bdb363c`)
- Model: thinkingmachines/inkling-2-mini; provider: PrimeIntellect
- Status: failed (output_truncated)
- Wall time: 50.89 s
- Turns: 3; tool calls: 2 {'other_tool': 2}
- Tokens: input 5346, cached 3200, output 16096, reasoning 16010, total 21442
- Cost: $0.0050

## Per-turn

| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |
|---|---|---|---|---|---|---|---|---|
| 1 | 1.1 | 1098 | 1412 | 0 | 54 | 10 | 0.0001221 | 1 |
| 2 | 1.8 | 674 | 1791 | 1408 | 42 | 0 | 6.2445e-05 | 1 |
| 3 | 45.7 | 43911 | 2143 | 1792 | 16000 | 16000 | 0.004853205 | 0 |

## TDD analysis

- **Verdict: no tests**
- First test file written: turn -; first implementation written: turn -; tests written first: False
- First test run: turn -, failed first (red): -
- Red to green cycles: 0; turns to first green: -; final tests green: -
- Rewrites per file: none

| test run turn | passed | ran | failures | errors |
|---|---|---|---|---|

## Hidden acceptance

**0 / 45 passed** (45 failed)
- `gutter game`: expected 0, got import failed: ModuleNotFoundError: No module named 'alley'
- `all ones`: expected 12, got import failed: ModuleNotFoundError: No module named 'alley'
- `single spare`: expected 12, got import failed: ModuleNotFoundError: No module named 'alley'
- `single spare then strike`: expected 18, got import failed: ModuleNotFoundError: No module named 'alley'
- `single strike`: expected 14, got import failed: ModuleNotFoundError: No module named 'alley'
- `perfect game`: expected 144, got import failed: ModuleNotFoundError: No module named 'alley'
- `all spares`: expected 72, got import failed: ModuleNotFoundError: No module named 'alley'
- `all spares, fill 1`: expected 68, got import failed: ModuleNotFoundError: No module named 'alley'
- `strike then spares`: expected 75, got import failed: ModuleNotFoundError: No module named 'alley'
- `last frame open`: expected 91, got import failed: ModuleNotFoundError: No module named 'alley'
- `last frame strike, gutter fills`: expected 6, got import failed: ModuleNotFoundError: No module named 'alley'
- `last frame spare, max fill`: expected 12, got import failed: ModuleNotFoundError: No module named 'alley'
- `strike then open`: expected 12, got import failed: ModuleNotFoundError: No module named 'alley'
- `random game 0`: expected 75, got import failed: ModuleNotFoundError: No module named 'alley'
- `random game 1`: expected 10, got import failed: ModuleNotFoundError: No module named 'alley'
- `random game 2`: expected 10, got import failed: ModuleNotFoundError: No module named 'alley'
- `random game 3`: expected 74, got import failed: ModuleNotFoundError: No module named 'alley'
- `random game 4`: expected 77, got import failed: ModuleNotFoundError: No module named 'alley'
- `random game 5`: expected 68, got import failed: ModuleNotFoundError: No module named 'alley'
- `random game 6`: expected 9, got import failed: ModuleNotFoundError: No module named 'alley'

## The agent's own tests

The agent never ran its tests.

## Deliverables

- Present: none
- Missing: alley.py, test_alley.py, NOTES.md

## Variant versus canonical

| parameter | this variant | canonical |
|---|---|---|
| frames | 6 | 10 |
| pins | 6 | 10 |
| spare_bonus_rolls | 2 | 1 |
| strike_bonus_rolls | 3 | 2 |
| name: function | final_score | score |
| name: module | alley | bowling |
