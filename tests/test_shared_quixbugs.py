"""Self-test of the vendored QuixBugs pilot set (#63)."""

import importlib.util
import json
import shutil
import time
from pathlib import Path

import pytest

SET = Path(__file__).resolve().parents[1] / "shared-tasks" / "quixbugs-python"
UP = SET / "upstream"
PROGRAMS = sorted(p.stem for p in (SET / "files").glob("*.py"))


def load_grader():
    spec = importlib.util.spec_from_file_location("qb_grader", SET / "grader.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def grader(tmp_path, monkeypatch):
    g = load_grader()
    monkeypatch.setattr(g, "OUT", tmp_path)
    monkeypatch.setattr(g, "TIMEOUT", 5.0)
    return g


def test_six_programs_one_case_each():
    ids = sorted(e["id"] for e in json.loads((SET / "evals.json").read_text())["evals"])
    assert len(PROGRAMS) == 6 and sorted(load_grader().CASES) == PROGRAMS
    assert ids == [f"quixbugs-{p}" for p in PROGRAMS]


@pytest.mark.parametrize("name", PROGRAMS)
def test_vendored_files_are_upstream_bytes(name):
    assert (SET / "files" / f"{name}.py").read_bytes() == (UP / "python_programs" / f"{name}.py").read_bytes()


@pytest.mark.parametrize("name", PROGRAMS)
def test_embedded_cases_match_upstream(name):
    lines = [x for x in (UP / "json_testcases" / f"{name}.json").read_text().splitlines() if x.strip()]
    assert load_grader().CASES[name] == [json.loads(x) for x in lines]


@pytest.mark.parametrize("name", PROGRAMS)
def test_upstream_fix_scores_one(grader, tmp_path, name):
    shutil.copy(UP / "correct_python_programs" / f"{name}.py", tmp_path / f"{name}.py")
    assert grader.grade(f"quixbugs-{name}")[0] == 1.0


@pytest.mark.parametrize("name", PROGRAMS)
def test_unfixed_program_scores_below_one(grader, tmp_path, name):
    shutil.copy(SET / "files" / f"{name}.py", tmp_path / f"{name}.py")
    assert grader.grade(f"quixbugs-{name}")[0] < 1.0


@pytest.mark.parametrize(
    "content",
    [None, "this is not python", "def other():\n    pass\n", "def lis(arr):\n    while True:\n        pass\n"],
)
def test_missing_broken_or_hanging_output_scores_zero(grader, tmp_path, content):
    grader.TIMEOUT = 2.0
    if content is not None:
        (tmp_path / "lis.py").write_text(content)
    assert grader.grade("quixbugs-lis")[0] == 0.0


def test_other_ids_have_no_deterministic_check(grader):
    assert grader.grade("something-else")[0] is None
    assert grader.grade("quixbugs-not-a-program")[0] is None


def test_printed_pass_lines_cannot_score(grader, tmp_path):
    # The program prints PASS lines and fake records at import, then stays buggy.
    bug = (SET / "files" / "lis.py").read_text()
    forged = "".join(f"QBREC-0 {i} 0\n" for i in range(12))
    (tmp_path / "lis.py").write_text(f"print('PASS ' * 50)\nprint({forged!r})\n" + "def lis(arr):\n    return -1\n")
    assert grader.grade("quixbugs-lis")[0] == 0.0
    (tmp_path / "lis.py").write_text("print('PASS ' * 50)\n" + bug)
    assert grader.grade("quixbugs-lis")[0] < 1.0


def test_always_equal_result_cannot_score(grader, tmp_path):
    (tmp_path / "lis.py").write_text(
        "class Any:\n    def __eq__(self, other):\n        return True\n\n\ndef lis(arr):\n    return Any()\n"
    )
    assert grader.grade("quixbugs-lis")[0] == 0.0


def test_duplicate_records_fail_the_case(grader, tmp_path, monkeypatch):
    shutil.copy(UP / "correct_python_programs" / "lis.py", tmp_path / "lis.py")
    monkeypatch.setattr(grader, "RUNNER", grader.RUNNER.replace("_flush()", "_write(f'{_tag} {_i} {_rec}\\n')"))
    assert grader.grade("quixbugs-lis")[0] == 0.0


def test_program_cannot_read_expected_values_from_the_runner(grader, tmp_path):
    # The child is fed arguments only, so a lookup through __main__ finds no answers to replay.
    (tmp_path / "lis.py").write_text(
        "import __main__\n\n_seen = iter(getattr(__main__, '_cases', []))\n\n\n"
        "def lis(arr):\n    case = next(_seen)\n    return case[1] if len(case) == 2 else -1\n"
    )
    assert grader.grade("quixbugs-lis")[0] == 0.0


def test_forked_grandchild_is_killed_with_the_run(grader, tmp_path):
    marker = tmp_path / "late.txt"
    (tmp_path / "lis.py").write_text(
        "import os, time\n\n"
        "if os.fork() == 0:\n"
        "    os.close(1)\n"
        "    time.sleep(1.5)\n"
        f"    open({str(marker)!r}, 'w').write('rewritten')\n"
        "    os._exit(0)\n\n\n"
        "def lis(arr):\n    return -1\n"
    )
    assert grader.grade("quixbugs-lis")[0] == 0.0
    time.sleep(2.5)
    assert not marker.exists()
