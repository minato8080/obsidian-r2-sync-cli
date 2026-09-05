#!/usr/bin/env python3
"""Dependency-free R2 sync entrypoint for iOS/a-Shell."""

import os
import sys

from r2sync import ParallelInterrupted
from r2sync.cli import main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ParallelInterrupted:
        print("\n中断しました。", file=sys.stderr, flush=True)
        os._exit(130)
    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr, flush=True)
        raise SystemExit(130)
