"""Idle-presenter watchdog tests.

Verifies the CPU-watch that restarts a stuck idle browser:
``_idle_chrome_ticks`` parses /proc/<pid>/stat utime+stime for every
Chromium process under the idle profile dir, and the budget check
distinguishes a stuck renderer (~1.8 cores) from a normal idle page
(~5% of a core) — the case observed on prod (ceki-provider-prod, 180% CPU
for 20h until manual restart).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider import daemon as d


def _fake_stat(utime, stime):
    """Build a /proc/<pid>/stat line with known utime(14)/stime(15)."""
    fields = ["0"] * 52
    fields[0] = "12345"
    fields[1] = "(chrome)"
    fields[2] = "R"
    fields[13] = str(utime)
    fields[14] = str(stime)
    return " ".join(fields)


class _FakePath:
    """Replaces daemon.Path with a /proc stat reader keyed by pid."""

    stats = {}

    def __init__(self, path):
        self.path = path

    def read_text(self):
        pid = self.path.split("/")[2]
        if pid in self.stats:
            return self.stats[pid]
        raise FileNotFoundError(pid)


def test_idle_chrome_ticks_filters_by_profile_dir(monkeypatch):
    """Only Chromium procs under the idle profile dir are counted."""
    profile = "/tmp/ceki-idle-abc"
    _FakePath.stats = {
        "111": _fake_stat(1000, 500),
        "222": _fake_stat(2000, 300),
        "333": _fake_stat(1, 1),
    }
    monkeypatch.setattr(d, "Path", _FakePath)
    monkeypatch.setattr(
        d,
        "_cmdlines",
        lambda: [
            ("111", f"--user-data-dir={profile} --type=renderer"),
            ("222", f"--user-data-dir={profile} --type=gpu-process"),
            ("333", "--user-data-dir=/sessions/other --type=renderer"),
            ("444", "not chrome"),
        ],
    )
    assert d._idle_chrome_ticks(profile) == {111: (1000, 500), 222: (2000, 300)}


def test_cpu_budget_catches_stuck_renderer():
    """A renderer burning ~1.8 cores exceeds the 0.75-core budget."""
    # Two samples of one renderer's utime/stime, 30s apart, SC_CLK_TCK=100.
    base = {111: (1000, 500)}
    now = {111: (1000 + 3600, 500 + 1800)}  # +5400 jiffies = 1.8 cores
    deltas = {pid: (now[pid][0] - u1, now[pid][1] - s1) for pid, (u1, s1) in base.items()}
    jiffies = sum(u + s for u, s in deltas.values())
    elapsed_jiffies = 30 * os.sysconf("SC_CLK_TCK")
    ratio = jiffies / elapsed_jiffies
    assert ratio > 0.75  # stuck renderer must trigger restart
    assert 1.7 <= ratio <= 1.9  # ~1.8 cores


def test_cpu_budget_spares_idle_page():
    """A normal idle page (~5% of a core) stays below the budget."""
    base = {111: (1000, 500)}
    now = {111: (1000 + 100, 500 + 50)}  # +150 jiffies over 30s = 0.05 cores
    deltas = {pid: (now[pid][0] - u1, now[pid][1] - s1) for pid, (u1, s1) in base.items()}
    jiffies = sum(u + s for u, s in deltas.values())
    elapsed_jiffies = 30 * os.sysconf("SC_CLK_TCK")
    ratio = jiffies / elapsed_jiffies
    assert ratio <= 0.75  # idle page must NOT trigger restart
