"""Python logging and terminal capture.

Fixes the duplicate-ERROR bug: the old `StdoutStderrFilter.filter()` returned
True unconditionally, so with no upper bound on the stdout handler every ERROR
record was emitted by *both* handlers. Since TerminalCapture tees both streams
into one file, every traceback was written twice.
"""
from __future__ import annotations

import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


class _BelowError(logging.Filter):
    """Keep INFO/WARNING on stdout; ERROR+ goes to stderr only."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


def setup_logging(level: int = logging.INFO) -> None:
    """Route INFO+ to stdout and ERROR+ to stderr, with no overlap."""
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(level)
    stdout_handler.addFilter(_BelowError())

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)

    fmt = UTCFormatter(
        "%(asctime)s.%(msecs)03dZ [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    stdout_handler.setFormatter(fmt)
    stderr_handler.setFormatter(fmt)

    logging.basicConfig(
        level=level, handlers=[stdout_handler, stderr_handler], force=True
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _TeeStream:
    """Write each message to both the original stream and a log file."""

    def __init__(self, original, log_fp):
        self._original = original
        self._log_fp = log_fp

    def write(self, data):
        self._original.write(data)
        self._original.flush()
        self._log_fp.write(data)
        self._log_fp.flush()

    def flush(self):
        self._original.flush()
        self._log_fp.flush()


class TerminalCapture:
    """Capture stdout+stderr into a file for the duration of one run.

    Note the rendered prompts are deliberately NOT printed to stdout any more —
    one measured run produced a 2.1 MB terminal log for 49 KB of useful output,
    because the 138k-char trajectory was echoed once per category. Full prompts
    are written once to the run's prompts/ directory instead.
    """

    def __init__(self, log_path: str | Path):
        self.log_path = Path(log_path)
        self._fp = None
        self._old = None

    def __enter__(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(self.log_path, "a", encoding="utf-8", buffering=1)
        self._fp.write(f"\n{'=' * 90}\nTerminal capture started: {utc_now_iso()}\n{'=' * 90}\n")
        self._old = (sys.stdout, sys.stderr, sys.excepthook)

        sys.stdout = _TeeStream(sys.__stdout__, self._fp)
        sys.stderr = _TeeStream(sys.__stderr__, self._fp)

        def excepthook(exc_type, exc, tb):
            print("\n[UNCAUGHT EXCEPTION]", file=sys.stderr)
            traceback.print_exception(exc_type, exc, tb, file=sys.stderr)

        sys.excepthook = excepthook
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            print("\n[EXCEPTION IN RUN]", file=sys.stderr)
            traceback.print_exception(exc_type, exc, tb, file=sys.stderr)

        sys.stdout, sys.stderr, sys.excepthook = self._old
        if self._fp:
            self._fp.write(f"\nTerminal capture ended: {utc_now_iso()}\n")
            self._fp.close()
        return False
