---
name: sla-calculator
description: Turn a priority into concrete acknowledge / resolve deadlines using helpdesk business hours
---

# SLA Calculator

1. Retrieve the response and resolution targets via it-tools (retrieve_it_policy).
2. Apply business hours (08:00-18:00 local, business days) for P2-P4; P1 is 24x7.
3. Show the calculation, e.g. "P2 reported 17:00 Friday -> acknowledged by 11:00 Monday".

## Output Format

| Priority | Acknowledge by | Resolve target |
|---|---|---|
| ... | ... | ... |

Always cite the policy section the targets come from.
