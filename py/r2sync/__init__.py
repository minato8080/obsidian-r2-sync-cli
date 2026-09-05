"""Dependency-free Python sync implementation for iOS/a-Shell."""


class PullError(Exception):
    """An expected, user-facing sync failure."""


class ParallelInterrupted(KeyboardInterrupt):
    """Ctrl+C received while worker threads may still be blocked."""
