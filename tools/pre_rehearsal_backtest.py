"""Backtest the pre-rehearsal's local THELMA predictions against a live rehearsal of the same release.

    .venv/bin/python3 tools/pre_rehearsal_backtest.py --profile default \\
        --release s3://<release bucket>/customizer-releases/<project>/<version>/release.zip \\
        --rehearsal docs/replace-generic/evidence/<run>/rehearsal-r1-not-ready.json --out build/backtest/<name>

Every synced release is kept in the release bucket, so a past rehearsal's exact pack (prompts, questions,
tools, knowledge base, teaching block) and evaluator code are at hand. The release's probe cases run
locally (``pre_rehearsal.run`` with the release's own ``evaluators/thelma_eval``), and each
prompt_fixable / retrieval_gap phenomenon's local prediction is set next to the live verdict, with the
live and local first-search metrics. Writes ``backtest.json`` under ``--out``; prints one row per phenomenon.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer import pre_rehearsal, pre_thelma  # noqa: E402


def release_dir(source: str, work: Path, session) -> Path:
    """The release unpacked under ``work`` (from an s3:// release.zip, a local zip or a directory)."""
    if Path(source).is_dir():
        return Path(source)
    if source.startswith("s3://"):
        bucket, key = source[5:].split("/", 1)
        raw = session.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    else:
        raw = Path(source).read_bytes()
    target = work / "release"
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        archive.extractall(target)
    return target


def scenario_of(pack_dir: Path, live: dict) -> dict:
    """The scenario fields the predictions read, from the release's compiled pack; the pack drops a gap's
    mechanism and a phenomenon's design, which the live rehearsal document records."""
    pack = json.loads((pack_dir / "pack.json").read_text(encoding="utf-8"))
    practice = json.loads((pack_dir / "golden" / "practice.json").read_text(encoding="utf-8"))
    recorded = {p["id"]: p for p in live.get("phenomena") or []}
    teaching = {**pack["teaching"], "phenomena": [
        {**p, **{k: recorded[p["id"]][k] for k in ("mechanism", "design") if k in recorded.get(p["id"], {})}}
        for p in pack["teaching"]["phenomena"]]}
    return {"language": pack.get("language"), "labs": {"teaching": teaching},
            "evaluation": {"goldenSet": practice, "retrievalToolName": pack.get("retrievalToolName"),
                           "judgeModel": pack.get("judgeModel"), "l1": pack.get("l1") or {}}}


def compare(local: list[dict], live: dict) -> list[dict]:
    by_id = {p["id"]: p for p in live.get("phenomena") or []}
    rows = []
    for p in local:
        if p["kind"] == "refusal":  # L1: the local runs' optimized focus checks against the live verdict
            lp = by_id.get(p["id"]) or {}
            rows.append({"id": p["id"], "kind": p["kind"], "local": p["prediction"], "live": lp.get("verdict"),
                         "agree": {"likely_reproduced": "reproduced", "likely_not_reproduced": "not_reproduced"}.get(p["prediction"]) == lp.get("verdict"),
                         "cases": [{"caseId": c["caseId"], "local": c.get("optimizedFocus"), "localTools": c.get("optimizedTools"),
                                    "live": ((lp.get("cases") or [{}])[0]).get("code"), "localCodes": [], "liveMetrics": {},
                                    "localMetrics": [], "localFirstSearch": []} for c in p.get("cases") or []]})
            continue
        if p["kind"] not in pre_thelma.THELMA_KINDS:
            continue
        lp = by_id.get(p["id"]) or {}
        live_cases = {c["caseId"]: c for c in lp.get("cases") or []}
        rows.append({
            "id": p["id"], "kind": p["kind"], "local": p["prediction"], "live": lp.get("verdict"),
            "agree": {"likely_reproduced": "reproduced", "likely_not_reproduced": "not_reproduced"}.get(p["prediction"]) == lp.get("verdict"),
            "cases": [{"caseId": c["caseId"], "local": c["prediction"], "localCodes": [v["code"] for v in c["pairs"]],
                       "live": (live_cases.get(c["caseId"]) or {}).get("code"),
                       "liveMetrics": {k: (live_cases.get(c["caseId"]) or {}).get(k) for k in ("baseline", "optimized")},
                       "localMetrics": [{k: v.get(k) for k in ("baseline", "optimized")} for v in c["pairs"]],
                       "localFirstSearch": [v["firstSearch"] for v in c["pairs"]]} for c in p["cases"]],
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--release", required=True, help="s3://…/release.zip, a local release.zip or an unpacked release")
    parser.add_argument("--rehearsal", type=Path, required=True, help="the live rehearsal.json of that release")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skills", action="store_true",
                        help="announce the pack's skills in the local system prompt (off: before 2026-10-01 no generated "
                             "pack's skill loaded live, their SKILL.md had no frontmatter)")
    args = parser.parse_args(argv)

    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    args.out.mkdir(parents=True, exist_ok=True)
    release = release_dir(args.release, args.out, session)
    pack_dir = release / "pack"
    live = json.loads(args.rehearsal.read_text(encoding="utf-8"))
    data = scenario_of(pack_dir, live)
    inputs = pre_rehearsal.inputs_from_pack(pack_dir, data)
    probes = [cid for p in pre_rehearsal.teaching.phenomena(data, pre_thelma.THELMA_KINDS) for cid in p.get("caseIds") or []]
    judge = str(data["evaluation"].get("judgeModel") or pre_rehearsal.DEFAULT_MODEL)
    thelma_dir = release / "evaluators" / "thelma_eval"
    client = session.client("bedrock-runtime", config=Config(read_timeout=300, retries={"max_attempts": 4, "mode": "standard"}))
    cases = probes + [c["id"] for c in inputs.practice if c["id"] not in probes and c.get("category") == "prohibited"]
    doc = pre_rehearsal.run(inputs, client, case_ids=cases, repeat=args.repeat, cache=args.out, skills=args.skills,
                            thelma=lambda t: pre_thelma.score(t, thelma_dir=thelma_dir, model=judge, profile=args.profile,
                                                              region=args.region))
    rows = compare(doc["phenomena"], live)
    (args.out / "backtest.json").write_text(json.dumps({"release": args.release, "rehearsal": str(args.rehearsal),
                                                       "rows": rows, "pre": doc}, ensure_ascii=False, indent=1) + "\n",
                                            encoding="utf-8")
    for r in rows:
        print(f"{'OK ' if r['agree'] else 'XX '} {r['id']:<34} {r['kind']:<15} local {r['local']:<22} live {r['live']}")
        for c in r["cases"]:
            if r["kind"] == "refusal":
                print(f"      {c['caseId']}: live {c['live']} | local optimized {c['local']} tools {c['localTools']}")
                continue
            live_m = c["liveMetrics"].get("baseline") or {}
            local_m = [((m.get("baseline") or {}).get("SQC"), (m.get("baseline") or {}).get("GR")) for m in c["localMetrics"]]
            print(f"      {c['caseId']}: live {c['live']} SQC {live_m.get('SQC')} GR {live_m.get('GR')} | local {c['localCodes']} "
                  f"(SQC, GR) {local_m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
