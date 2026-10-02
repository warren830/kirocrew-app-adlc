"""Count model tokens once when OTel wrappers repeat their children's usage."""
import math


def count_tokens(spans):
    parents, usage = {}, {}
    for span in spans:
        sid = span.get("spanId") or span.get("span_id")
        if not sid:
            continue
        parents[sid] = span.get("parentSpanId") or span.get("parent_span_id")
        attrs = span.get("attributes") or {}

        def value(keys):
            for key in keys:
                number = attrs.get(key)
                if isinstance(number, (int, float)) and math.isfinite(number) and number >= 0:
                    return int(number)
            return 0

        incoming = value(("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "inputTokens"))
        outgoing = value(("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "outputTokens"))
        if incoming or outgoing:
            usage[sid] = (incoming, outgoing)
    ancestors = set()
    for sid in usage:
        seen = {sid}
        parent = parents.get(sid)
        while parent and parent not in seen:
            ancestors.add(parent)
            seen.add(parent)
            parent = parents.get(parent)
    leaves = [counts for sid, counts in usage.items() if sid not in ancestors]
    return sum(x[0] for x in leaves), sum(x[1] for x in leaves)
