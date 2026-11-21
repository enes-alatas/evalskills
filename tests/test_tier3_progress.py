"""#239: a live trial's output reaches the operator's terminal as it arrives, cleaned, and run.log is unchanged."""

import io
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import tier3  # noqa: E402


class Sink(io.StringIO):
    """Stands in for stderr. It notes, for each line, whether the trial was still running when it arrived."""

    def __init__(self, proc_ref):
        super().__init__()
        self.proc_ref, self.seen = proc_ref, []

    def write(self, s):
        p = self.proc_ref[0]
        if s.strip():
            self.seen.append((s.rstrip("\n"), p is not None and p.poll() is None))
        return super().write(s)


def trial(code: str, log_f):
    return subprocess.Popen([sys.executable, "-I", "-c", code], stdout=log_f, stderr=subprocess.STDOUT)


def test_lines_are_shown_while_the_trial_runs():
    ref = [None]
    sink = Sink(ref)
    code = "import time\nprint('installing agent', flush=True)\ntime.sleep(2.5)\nprint('trial 1 done', flush=True)"
    with tempfile.TemporaryFile() as log_f:
        ref[0] = trial(code, log_f)
        got, why = tier3._wait(ref[0], log_f, time.monotonic(), tier3.progress_for(log_f, "pilot-x-1", sink))
        log_f.seek(0)
        log = log_f.read()
    assert (got, why) == (0, None)
    assert sink.seen[0] == ("[pilot-x-1] installing agent", True)  # before the trial ended
    assert [line for line, _ in sink.seen] == ["[pilot-x-1] installing agent", "[pilot-x-1] trial 1 done"]
    assert log == b"installing agent\ntrial 1 done\n"  # run.log gets the same bytes, untouched


def test_escapes_and_controls_are_removed():
    assert tier3.progress_text(b"\x1b[31mred\x1b[0m \x1b]0;title\x07done") == "red done"
    assert tier3.progress_text(b"10%\r50%\r100%\r") == "100%"  # a redraw shows what ends up on screen
    assert tier3.progress_text(b"a\x00b\x07c\x08d\x7fe\tf") == "abcde f"
    assert tier3.progress_text("c1\x9b31m".encode()) == "c131m"  # an 8-bit CSI, as a character
    assert tier3.progress_text(b"raw\x9bbyte") == "raw\ufffdbyte"  # invalid UTF-8: a replacement character
    assert tier3.progress_text("safe‮txt.exe​".encode()) == "safetxt.exe"  # bidi and zero-width
    assert tier3.progress_text(b"\x1bPq#0;1;2~\x1b\\left") == "left"  # a DCS string
    assert tier3.progress_text(b"\x1b[2J\x1b[H") == ""
    assert len(tier3.progress_text(b"x" * 5000)) == tier3.PROGRESS_WIDTH


def test_an_empty_or_cleaned_away_line_is_not_shown():
    out = io.StringIO()
    with tempfile.TemporaryFile() as log_f:
        log_f.write(b"\n\x1b[2K\r\n   \nreal line\n")
        log_f.flush()
        tier3.progress_for(log_f, "r", out).pump(final=True)
    assert out.getvalue() == "[r] real line\n"


def test_a_burst_is_cut_and_counted(monkeypatch):
    monkeypatch.setattr(tier3, "PROGRESS_LINES_PER_PUMP", 3)
    out = io.StringIO()
    with tempfile.TemporaryFile() as log_f:
        log_f.write(b"".join(b"line %d\n" % i for i in range(10)))
        log_f.flush()
        tier3.progress_for(log_f, "r", out).pump()
    shown = out.getvalue().splitlines()
    assert shown == ["[r] line 0", "[r] line 1", "[r] line 2", "[r] … 7 more line(s) not shown, kept in run.log"]


def test_a_line_with_no_end_is_shown_in_pieces(monkeypatch):
    monkeypatch.setattr(tier3, "PROGRESS_MAX_PARTIAL", 10)
    out = io.StringIO()
    with tempfile.TemporaryFile() as log_f:
        log_f.write(b"y" * 25)
        log_f.flush()
        p = tier3.progress_for(log_f, "r", out)
        p.pump()
        assert out.getvalue() == "[r] " + "y" * 25 + "\n"  # one read of 25 bytes, held past the cap, shown
        p.pump(final=True)
    assert out.getvalue().count("\n") == 1


def test_the_log_cap_still_stops_a_flooding_trial(monkeypatch):
    monkeypatch.setattr(tier3, "MAX_LOG_BYTES", 200_000)
    out = io.StringIO()
    code = "import sys\nwhile True:\n    sys.stdout.write('flood ' * 20 + '\\n')"
    with tempfile.TemporaryFile() as log_f:
        p = trial(code, log_f)
        try:
            got, why = tier3._wait(p, log_f, time.monotonic(), tier3.progress_for(log_f, "r", out))
        finally:
            p.kill()
            p.wait()
    assert got is None and why == "stopped: its output passed 200000 bytes"
    assert len(out.getvalue()) < 200_000 // 10  # what reached the terminal is bounded, the rest is counted


def test_progress_off_passes_nothing(monkeypatch):
    monkeypatch.setattr(tier3, "PROGRESS", False)
    with tempfile.TemporaryFile() as log_f:
        assert tier3.progress_for(log_f, "r", io.StringIO()) is None


class Broken(io.StringIO):
    """A terminal that went away: every write fails, as with a closed `tee` or a lost tty."""

    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")


def test_a_failing_display_never_changes_the_outcome(capsys):
    code = "import time\nprint('one', flush=True)\ntime.sleep(1.5)\nprint('two', flush=True)\nraise SystemExit(3)"
    with tempfile.TemporaryFile() as log_f:
        p = trial(code, log_f)
        got, why = tier3._wait(p, log_f, time.monotonic(), tier3.progress_for(log_f, "r", Broken()))
        log_f.seek(0)
        log = log_f.read()
    assert (got, why) == (3, None)  # the child's own exit code, so run() goes on to stop_trial and cleanup
    assert log == b"one\ntwo\n"
    assert capsys.readouterr().err.count("progress display stopped: BrokenPipeError") == 1  # then it stays off


def test_a_failure_in_the_final_pump_keeps_the_exit_code(monkeypatch):
    class FailsLast(tier3._Progress):
        def pump(self, final=False):
            if final:
                raise OSError(5, "Input/output error")

    with tempfile.TemporaryFile() as log_f:
        p = trial("print('done')", log_f)
        assert tier3._wait(p, log_f, time.monotonic(), FailsLast(log_f.fileno(), "r", io.StringIO())) == (0, None)
