# Skill: test-driven development

Write the test before the code it tests. Every cycle has three steps.

1. **Red.** Add one or a few tests for the next requirement to the test file. Run the tests. They must fail
   (or error because the function does not exist yet). If a new test passes straight away, it is not testing
   anything new: change it or move on to the next requirement.
2. **Green.** Write the simplest code that makes the failing tests pass. Run the tests again. Repeat until all
   tests pass.
3. **Refactor.** Tidy names and remove duplication without changing behaviour. Run the tests again; they must
   still pass.

Rules:

- The very first file you write is the test file, and the first test run must fail.
- Start with the simplest case (for example an empty or zero case), then add the cases the task describes one
  rule at a time, then the error cases.
- One requirement at a time. Name each test after the requirement it covers, for example `test_r3_spare_bonus`.
- Use concrete expected values you have worked out by hand from the task text, not values computed by the code
  under test.
- Cover every error case the task lists with `assertRaises`.
- When a test fails, read the failure message carefully before changing code. Decide whether the test or the code
  is wrong by going back to the task text.
