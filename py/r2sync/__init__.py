"""Dependency-free Python sync implementation for iOS/a-Shell."""

__version__ = "1.0.0"


class PullError(Exception):
    """An expected, user-facing sync failure."""


class ParallelInterrupted(KeyboardInterrupt):
    """Ctrl+C received while worker threads may still be blocked."""
