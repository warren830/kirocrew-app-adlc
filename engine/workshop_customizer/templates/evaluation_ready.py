"""A successful evaluation request can precede usable, fully indexed span data."""
import json
import math
import sys


def has_usable_score(result):
    if not result.get("success"):
        return False
    for evaluation in (result.get("run") or {}).get("results", []):
        for score in evaluation.get("sessionScores", []):
            value = score.get("value")
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value)
                    and str(score.get("label", "")).lower() not in ("skipped", "error")):
                return True
    return False


if __name__ == "__main__":
    try:
        ready = has_usable_score(json.load(sys.stdin))
    except (ValueError, TypeError, AttributeError):
        ready = False
    raise SystemExit(0 if ready else 1)
