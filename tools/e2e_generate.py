"""Headless acceptance driver for the KiroCrew generation loop (SPEC D11): a thin wrapper.

    tools/e2e_generate.py --brief acceptance/briefs/ops-support.md --project-id ops-support \
        --pack-kind reference --review-reference --build --out build/e2e-generate/ops-support

is ``tools/kiro_generate.py --loop ...``: draft -> apply -> draft-mode Validate -> repair rounds
through the same routes functions and the process backend's Service, with the evidence log in
``<out>/report.json`` and ``<out>/report.md``.  See tools/kiro_generate.py for every option.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kiro_generate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return kiro_generate.main(args if "--loop" in args else ["--loop", *args])


if __name__ == "__main__":
    raise SystemExit(main())
