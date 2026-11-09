# QuixBugs (Python), pilot subset

- **Upstream:** https://github.com/jkoppel/QuixBugs at commit `4257f44b0ff1181dedaedee6a447e133219fcebf`
- **Authors and paper:** Derrick Lin, James Koppel, Angela Chen and Armando Solar-Lezama (MIT CSAIL). "QuixBugs: A
  Multi-Lingual Program Repair Benchmark Set Based on the Quixey Challenge", SPLASH Companion 2017.
- **Licence:** MIT, copyright 2017-2019 James Koppel. The full text is in `upstream/LICENSE`.
- **Approved:** Enes, 2026-11-09 (#63), under ADR-0006.

## What is vendored, unchanged
- `files/<program>.py` and `upstream/python_programs/<program>.py`: upstream's buggy programs, byte for byte.
- `upstream/correct_python_programs/<program>.py`: upstream's fixed programs. They are used only by our self-test and
  are never staged for the agent (`pipeline/tier3.py` copies `evals.json`, `grader.py`, `config.yml` and `files/`).
- `upstream/json_testcases/<program>.json`: upstream's test cases. `grader.py` embeds the same lines, and a test
  checks that they match.

Programs: `find_first_in_sorted`, `lis`, `next_permutation`, `quicksort`, `sieve` and `to_base`. All six have a
JSON test file and a plain `fn(*args) == expected` upstream test, and none needs the graph `Node` helper.

## What was adapted
- **Prompt:** upstream has no agent prompts. Each case says the program has a bug on exactly one line (which is
  upstream's own premise), asks for a minimal fix, and names the input and output paths.
- **Grading:** upstream runs its cases with pytest (`python_testcases/test_<program>.py`). `grader.py` runs the same
  cases with the stdlib, the same way (call with the case's arguments, compare with `==`), in a fresh interpreter
  with a timeout. The score is the fraction of cases that pass, not pytest's pass/fail.
- **Result handling:** the program's return value crosses a process boundary as JSON and is compared in the
  parent, so a tuple would compare equal to a list (upstream's `==` would not). None of the six programs' expected
  values is a tuple, so no upstream case changes outcome.
- **Grader residual:** the program under test runs in the trial container, where `/tests/grader.py` (with the
  embedded expected values) is on disk at verification time. A program could read it and replay the answers. The
  backstop is the LLM-judged assertion that only the buggy line changed; a lookup table fails it.
- **Formatting:** the vendored files are excluded from ruff, so they stay as upstream ships them.

QuixBugs dates from 2017 and is widely published, so models may have seen these programs and their fixes. Treat
results on it as a pilot of the pipeline, not as a contamination-free measure.
