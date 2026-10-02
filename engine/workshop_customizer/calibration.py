"""Judge-noise calibration → ``evaluation.noiseBand``.

The upstream workshop's ``13-judge-stability.sh`` re-scores one trace several times with the judge
(THELMA RAG quality) and prints the scores. This module turns that output into a per-metric noise
band that the pack records under ``evaluation.noiseBand``: a score delta smaller than the band is
noise, not an improvement. Authority stays with the evaluator — this hook only records what the
judge did.

Accepted inputs
- the raw stdout of ``13-judge-stability.sh`` (lines ``value = 0.83`` and/or ``分数: [0.83, 0.8]``)
- a plain list of numbers (one per line, or JSON array)

Band rule (deterministic, documented in the plan): ``band = max(2 * population_std, spread, floor)``
rounded to three decimals, with ``floor = 0.02``; verdict thresholds mirror the upstream script
(std ≤ 0.03 stable, ≤ 0.08 moderate, else unstable).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

VALUE_LINE = re.compile(r"^\s*value\s*=\s*([0-9]*\.?[0-9]+)\s*$", re.M)
SCORES_LINE = re.compile(r"分数:\s*\[([^\]]*)\]")
DEFAULT_METRIC = "thelma_rag_quality"
BAND_FLOOR = 0.02
STABLE_STD = 0.03
MODERATE_STD = 0.08
MIN_RUNS = 2


class CalibrationError(Exception):
    pass


@dataclass(frozen=True)
class Stability:
    metric: str
    values: list[float]
    mean: float
    std: float
    spread: float
    band: float
    verdict: str  # stable | moderate | unstable

    @property
    def runs(self) -> int:
        return len(self.values)


def parse_scores(text: str) -> list[float]:
    """Scores from a stability-script log, a JSON array, or one number per line."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CalibrationError(f"not a JSON array of scores: {exc}") from exc
        return [float(x) for x in data]
    values = [float(v) for v in VALUE_LINE.findall(text)]
    if values:
        return values
    match = SCORES_LINE.search(text)
    if match:
        return [float(x) for x in match.group(1).split(",") if x.strip()]
    plain: list[float] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            plain.append(float(line))
        except ValueError:
            return []
    return plain


def summarise(values: list[float], *, metric: str = DEFAULT_METRIC, floor: float = BAND_FLOOR) -> Stability:
    if len(values) < MIN_RUNS:
        raise CalibrationError(f"need at least {MIN_RUNS} scores, got {len(values)}")
    for v in values:
        if not 0.0 <= v <= 1.0:
            raise CalibrationError(f"score {v} outside [0, 1]")
    mean = sum(values) / len(values)
    var = sum((x - mean) ** 2 for x in values) / len(values)
    std = var ** 0.5
    spread = max(values) - min(values)
    band = round(min(1.0, max(2 * std, spread, floor)), 3)
    verdict = "stable" if std <= STABLE_STD else "moderate" if std <= MODERATE_STD else "unstable"
    return Stability(metric=metric, values=list(values), mean=round(mean, 4), std=round(std, 4), spread=round(spread, 4), band=band, verdict=verdict)


def calibrate(text: str, *, metric: str = DEFAULT_METRIC) -> Stability:
    values = parse_scores(text)
    if not values:
        raise CalibrationError("no scores found (expected 'value = x' lines, a '分数: [...]' line, a JSON array, or one number per line)")
    return summarise(values, metric=metric)


def apply_to_scenario(scenario_path: Path, stability: Stability, *, source: str = "13-judge-stability.sh", now: datetime | None = None) -> dict:
    """Write ``evaluation.noiseBand[metric]`` (schema: per-metric number) and record evidence in ``labs.observations``."""
    data = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "evaluation" not in data:
        raise CalibrationError("scenario has no evaluation block")
    evaluation = data["evaluation"]
    band = evaluation.setdefault("noiseBand", {}) or {}
    band[stability.metric] = stability.band
    evaluation["noiseBand"] = band
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    note = (
        f"Judge noise calibration ({source}, {stamp}): {stability.metric} over {stability.runs} runs — "
        f"mean {stability.mean:.3f}, std {stability.std:.3f}, spread {stability.spread:.3f} → noiseBand {stability.band:.3f} ({stability.verdict}). "
        "A delta inside the band is not a real change."
    )
    labs = data.setdefault("labs", {}) or {}
    observations = [o for o in (labs.get("observations") or []) if not str(o).startswith(f"Judge noise calibration (") or stability.metric not in str(o)]
    observations.append(note)
    labs["observations"] = observations
    data["labs"] = labs
    scenario_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    return {"metric": stability.metric, "noiseBand": stability.band, "verdict": stability.verdict, "runs": stability.runs, "note": note, **asdict(stability)}


def is_real_change(delta: float, band: dict[str, float] | float, metric: str = DEFAULT_METRIC) -> bool:
    """True when |delta| clears the calibrated band for the metric (fail closed: unknown metric → not real)."""
    limit = band.get(metric) if isinstance(band, dict) else band
    if limit is None:
        return False
    return abs(delta) > float(limit)
