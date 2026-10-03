"""What the console's modules share: the time stamp they record, AWS's paged lists and error codes, and the route
wrappers that answer a refusal (the console's own, or AWS's) as an HTTP status instead of an internal error."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pages(call: Callable[..., Any], key: str, *, max_pages: int | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    """Every item under ``key`` of every page (``nextToken``), or of the first ``max_pages`` (live: ListRegistries
    answers an empty first page that still carries a nextToken)."""
    out: list[dict[str, Any]] = []
    read = 0
    while max_pages is None or read < max_pages:
        page = call(**kwargs)
        read += 1
        out += page.get(key) or []
        if not page.get("nextToken"):
            break
        kwargs["nextToken"] = page["nextToken"]
    return out


def error_code(exc: BaseException) -> str:
    """AWS's error code of a ClientError (``""`` for anything else)."""
    response = getattr(exc, "response", None)
    return str(((response or {}).get("Error") or {}).get("Code") or "") if isinstance(response, dict) else ""


def aws_status(exc: BaseException) -> tuple[int, str] | None:
    """An AWS answer as the route's: its own 4xx (502 for a 5xx) and ``<code>: <message>``; None when not AWS's."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict) or "Error" not in response:
        return None
    error = response.get("Error") or {}
    status = int((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 500)
    return (status if 400 <= status < 500 else 502), f"{error.get('Code') or type(exc).__name__}: {error.get('Message') or exc}"


def _refused(exc: BaseException) -> tuple[int, dict[str, Any]]:
    """A console refusal: its ``status`` (400 by default) and ``extra`` fields, or a flow's ``issues`` when they are why."""
    issues = getattr(exc, "issues", None)
    return getattr(exc, "status", 400), {"error": str(exc), **(getattr(exc, "extra", None) or {}), **({"issues": issues} if issues else {})}


def safe(fn: Callable[[Any], Any], refusals: tuple[type[BaseException], ...],
         not_found: tuple[type[BaseException], ...] = ()) -> Callable[[Any], Any]:
    """A route whose ``refusals`` answer 400 and whose missing thing (``not_found``, or AWS's
    ResourceNotFoundException) answers 404 (not an internal error)."""
    def route(r: Any) -> Any:
        try:
            return fn(r)
        except refusals as exc:
            return _refused(exc)
        except not_found as exc:
            return 404, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) == "ResourceNotFoundException":
                return 404, {"error": f"not found: {str(exc)[:300]}"}
            raise
    return route


def guarded(fn: Callable[[Any], Any], refusals: tuple[type[BaseException], ...], *, limit: int | None = None) -> Callable[[Any], Any]:
    """A route whose refusals are answers: the console's own with their status, AWS's with theirs (502 for a 5xx; the
    message cut to ``limit``), a request botocore refuses a 400."""
    def run(r: Any) -> Any:
        try:
            return fn(r)
        except refusals as exc:
            return _refused(exc)
        except Exception as exc:  # noqa: BLE001 - only AWS's answers are mapped; anything else stays a 500
            if type(exc).__name__ == "ParamValidationError":
                return 400, {"error": " ".join(str(exc).split())[:600]}
            found = aws_status(exc)
            if found is None:
                raise
            return found[0], {"error": found[1][:limit]}

    run.__name__ = getattr(fn, "__name__", "route")
    return run
