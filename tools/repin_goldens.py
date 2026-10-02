"""Recompute the reference-pack golden pins after an intentional release change (for example a merge).

Rewrites tests/fixtures/golden-render/<pack>.json and the PINNED tuples in tests/test_golden_render.py
from the current render. Run it only when the release output changed on purpose, and say so in the commit.

    PYTHONPATH=engine .venv/bin/python3 tools/repin_goldens.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer import compiler, render  # noqa: E402
from workshop_customizer.scenario import load_scenario  # noqa: E402

TEST = REPO / "tests" / "test_golden_render.py"
FIXTURES = REPO / "tests" / "fixtures" / "golden-render"
TEMPLATE_COMMIT = json.loads((REPO / "template-lock.json").read_text())["template"]["commit"]


def fingerprint(pack_id: str, base: Path) -> tuple[str, str, dict]:
    scenario = load_scenario(REPO / "scenarios" / pack_id / "scenario.yaml")
    pack = compiler.compile_pack(scenario, base / "out", template_commit=TEMPLATE_COMMIT)
    release = render.render_release(pack, REPO / "upstream", base / "out" / "release", template_commit=TEMPLATE_COMMIT)
    manifest = (release.release_dir / render.MANIFEST_NAME).read_bytes()
    pack_digest = hashlib.sha256(json.dumps(pack.files, sort_keys=True).encode("utf-8")).hexdigest()
    golden = {
        "version": release.version,
        "manifestSha256": hashlib.sha256(manifest).hexdigest(),
        "patches": [[p.file, p.name, p.matches] for p in release.patches],
        "files": dict(sorted(release.files.items())),
    }
    return release.version, pack_digest, golden


def main() -> int:
    text = TEST.read_text(encoding="utf-8")
    for pack_id in sorted(re.findall(r'^    "([a-z0-9-]+)": \("', text, flags=re.M)):
        with tempfile.TemporaryDirectory() as tmp:
            version, digest, golden = fingerprint(pack_id, Path(tmp))
        path = FIXTURES / f"{pack_id}.json"
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        current.update(golden, packId=pack_id)
        path.write_text(json.dumps(current, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        text, n = re.subn(rf'^    "{re.escape(pack_id)}": \("[^"]+", "[0-9a-f]+"\),$',
                          f'    "{pack_id}": ("{version}", "{digest}"),', text, flags=re.M)
        if n != 1:
            raise SystemExit(f"could not update the PINNED entry for {pack_id}")
        print(f"{pack_id}: {version} {digest[:12]}")
    TEST.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
