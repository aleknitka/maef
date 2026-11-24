# Skill: working in the Python sandbox

The sandbox has Python 3.12 with the standard library only. There is no network, no pip and no pytest. Only
`/workspace` is writable, and only `/workspace/out/` is kept after the run.

Write or replace a whole file (always send the complete file content, never a fragment):

    run_command  argv: ["sh", "-c", "mkdir -p /workspace/out && cat > /workspace/out/test_example.py"]
                 stdin: <complete file content>

Run the tests (unittest, verbose):

    run_command  argv: ["sh", "-c", "cd /workspace/out && python3 -m unittest -v 2>&1"]

Read a file back:

    run_command  argv: ["cat", "/workspace/out/example.py"]

Tips:

- `exit_code` 0 means every test passed. Otherwise read the `FAIL:` / `ERROR:` blocks in the output.
- Tests import the module by name (`import example` or `from example import ...`) because they run from
  `/workspace/out`.
- Keep the code to the standard library. Use type hints and short docstrings.
