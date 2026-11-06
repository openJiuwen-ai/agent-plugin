"""Terminal and OpenTelemetry stream-capture utilities. These helpers tee stdout/stderr
to log files and capture console-exported spans in memory."""
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
import io


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO string ending in Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

# -----------------------------------------------------------------------------
# Terminal capture helper (stdout + stderr + tracebacks)
# -----------------------------------------------------------------------------
# Stream wrapper that writes output to two places at once.
class TeeStream:
    """File-like wrapper that writes each stream message to both the original stream and a log file."""
    def __init__(self, original, log_fp):
        """Store the original stream and log-file handle that will receive duplicated output."""
        self._original = original
        self._log_fp = log_fp

    def write(self, data):
        """Write data to the terminal stream and the log file, flushing both immediately."""
        self._original.write(data)
        self._original.flush()
        self._log_fp.write(data)
        self._log_fp.flush()

    def flush(self):
        """Flush both wrapped output destinations."""
        self._original.flush()
        self._log_fp.flush()


# Context manager that captures terminal output while still showing it live.
class TerminalCapture:
    """Capture stdout+stderr into a file for the duration of a single run."""
    def __init__(self, log_path: str):
        """Prepare capture state for one terminal log path."""
        self.log_path = log_path
        self._fp = None
        self._old_stdout = None
        self._old_stderr = None
        self._old_excepthook = None

    def __enter__(self):
        """Start capturing stdout, stderr, and uncaught exceptions into the terminal log file."""
        path = Path(self.log_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        self._fp = open(path, "a", encoding="utf-8", buffering=1)
        self._fp.write("\n" + "=" * 90 + "\n")
        self._fp.write(f"Terminal capture started: {utc_now_iso()}\n")
        self._fp.write("=" * 90 + "\n")

        self._old_stdout = sys.stdout
        self._old_stderr = sys.stderr
        self._old_excepthook = sys.excepthook

        sys.stdout = TeeStream(sys.__stdout__, self._fp)
        sys.stderr = TeeStream(sys.__stderr__, self._fp)

        def excepthook(exc_type, exc, tb):
            """Write uncaught exception tracebacks into the captured stderr stream."""
            print("\n[UNCAUGHT EXCEPTION]", file=sys.stderr)
            traceback.print_exception(exc_type, exc, tb, file=sys.stderr)

        sys.excepthook = excepthook
        return self

    def __exit__(self, exc_type, exc, tb):
        """Restore the original streams/hooks, close the log file, and preserve normal exception behavior."""
        if exc_type is not None:
            print("\n[EXCEPTION IN RUN]", file=sys.stderr)
            traceback.print_exception(exc_type, exc, tb, file=sys.stderr)

        # Restore streams/hooks
        sys.stdout = self._old_stdout
        sys.stderr = self._old_stderr
        sys.excepthook = self._old_excepthook

        if self._fp:
            self._fp.write(f"\nTerminal capture ended: {utc_now_iso()}\n")
            self._fp.close()

        return False  # keep normal exception behavior

# ─────────────────────────────────────────────────────────────────────────────
# Magentic-One orchestration helpers
# ─────────────────────────────────────────────────────────────────────────────
# Capture OTel exporter output into memory while still forwarding it to the original stream.
class CaptureAndTeeStream:
   """Capture OTEL ConsoleSpanExporter output while still printing it."""

   def __init__(self, original):
        """Create an in-memory buffer and remember the optional stream to forward writes to."""
        self._original = original
        self._buffer = io.StringIO()

   def write(self, data):
        """Store exporter output in memory and optionally forward it to the original stream."""
        self._buffer.write(data)
        if self._original is not None:
            self._original.write(data)
            self._original.flush()

   def flush(self):
        """Flush the forwarded stream when one exists."""
        if self._original is not None:
            self._original.flush()

   def getvalue(self) -> str:
        """Return all captured exporter output as a string."""
        return self._buffer.getvalue()
