"""Fixture: forbidden calls (breakpoint, time.time_ns, subprocess kwargs)."""
import subprocess
import time


def f():
    breakpoint()  # BAD: breakpoint
    return time.time_ns()  # BAD: time_time_ns


def g():
    subprocess.run(["ls"], capture_output=True)  # BAD: subprocess_capture_output
    subprocess.run(["ls"], text=True)  # BAD: subprocess_text


def h():
    from subprocess import run
    run(["ls"], capture_output=True)  # BAD: subprocess_capture_output_aliased
