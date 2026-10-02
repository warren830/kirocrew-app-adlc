---
name: ticket-triage
description: Classify an IT problem into P1-P4, gather the facts the helpdesk needs, and look up the requester's own ticket
---

# Ticket Triage

When an employee reports a problem:

1. Retrieve the priority definitions via it-tools (retrieve_it_policy) and classify P1-P4.
2. Collect: what is broken, since when, how many people are affected, device asset tag if relevant.
3. If the employee names an existing ticket, use lookup_ticket with the employee's own requester id.
4. State the acknowledgement target for the priority and what the employee can do meanwhile.

## Output Format

- Priority and why
- Facts collected / still missing
- Next step (self-service action or ticket)
- Source policy section
