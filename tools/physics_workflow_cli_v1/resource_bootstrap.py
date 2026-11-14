"""Gate child creation until the supervisor owns this process tree in a Job Object."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time


def main() -> int:
    gate = Path(sys.argv[1])
    command = sys.argv[2:]
    deadline = time.monotonic() + 60.0
    while not gate.exists():
        if time.monotonic() > deadline:
            return 4
        time.sleep(0.02)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        creationflags=creationflags,
        stdout=sys.stdout.buffer,
        stderr=sys.stderr.buffer,
    )
    return process.wait()


if __name__ == "__main__":
    sys.exit(main())
