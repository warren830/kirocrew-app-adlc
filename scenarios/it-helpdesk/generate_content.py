"""Generate the fictional IT helpdesk pack content (knowledge documents, skills, prompts).

Northwind Robotics is an invented company; every policy, name and number below is synthetic and
exists only to exercise the workshop engine with a second, non-HR scenario. Like the upstream HR
sample, every knowledge document ends with an intentionally noisy, cross-domain FAQ section so the
retrieval-quality diagnosis (THELMA SP1-high / SP2-low) has something real to find.

Teaching devices (declared in scenario.yaml labs.teaching): the FAQ tails of vpn_access and
incident_priority_sla repeat the words "VPN session" in unrelated questions. They are bait for the
absent retrieval gap vpn-session-timeout: the knowledge base deliberately has no session-length
rule, so retrieval returns look-alike noise and the right answer is "not in the policy, open a
ticket". None of these entries may mention a timeout, idle time or a maximum session length.

Usage: python3 generate_content.py   (writes next to this file; deterministic)
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent

NOISE_FAQ = {
    "vpn_access": [
        ("Where can I find the cafeteria menu for this week?", "The menu is posted on the facilities intranet page every Monday."),
        ("How do I book a parking space at the Shenzhen campus?", "Use the facilities app; spaces are released at 07:00 each day."),
        ("Is the printer on floor 3 out of toner?", "Facilities replaces toner on request through the office-services form."),
        ("Is there a recorded VPN session for new joiners in the training calendar?", "The onboarding recording is listed in the learning portal under Getting connected."),
        ("Can I book the quiet room for a VPN session with a supplier?", "Quiet rooms are booked through the facilities app like any meeting room."),
    ],
    "identity_and_passwords": [
        ("When is the next town hall?", "Town halls are held on the last Thursday of each quarter."),
        ("Can I expense my mobile phone plan?", "Mobile reimbursement follows the travel and expense policy owned by Finance."),
        ("Where do I pick up my new badge photo?", "Badge photos are taken at reception between 09:00 and 11:00."),
    ],
    "incident_priority_sla": [
        ("How do I reserve a meeting room?", "Rooms are booked from the calendar client; recurring bookings need facilities approval."),
        ("What is the guest Wi-Fi password?", "Guest Wi-Fi vouchers are printed at reception and expire after 24 hours."),
        ("Where is the town-hall stream if I join from a VPN session at home?", "The town-hall stream is published on the intranet video page by Internal Communications."),
    ],
    "hardware_lifecycle": [
        ("Does the office gym open on weekends?", "The gym is open 06:00-22:00 on weekdays and closed on public holidays."),
        ("How do I order business cards?", "Business cards are ordered through the brand portal with manager approval."),
    ],
    "software_requests": [
        ("Where is the lost-and-found?", "Lost items are kept at reception for 30 days."),
        ("Can I bring my dog to the office?", "Pets are not permitted in laboratory areas; office areas follow the site policy."),
    ],
    "security_incidents": [
        ("How do I submit a holiday request?", "Holiday requests are submitted in the people system and approved by your manager."),
        ("Who do I ask about my payslip?", "Payslip questions go to the payroll mailbox, not to the IT helpdesk."),
    ],
}

DOCUMENTS = {
    "vpn_access": (
        "VPN Access Policy",
        [
            ("Scope", "This policy describes how Northwind Robotics employees connect to internal systems from outside the office network."),
            ("Requirements", "- VPN is available only from a company-managed device enrolled in device management.\n- Multi-factor authentication (MFA) is mandatory for every VPN session.\n- Personal devices (including personal tablets and phones) are not permitted on the VPN.\n- Contractors receive time-boxed VPN accounts that expire with their contract."),
            ("Self-service credential reset", "- Employees may reset their own VPN credentials once every 24 hours through the IT portal (Self-service > VPN > Reset).\n- New credentials are delivered to the corporate mailbox only.\n- A second reset inside the same 24-hour window requires a helpdesk ticket and identity verification."),
            ("Troubleshooting order", "1. Confirm the device is enrolled and up to date.\n2. Confirm MFA prompts arrive on the registered authenticator.\n3. Reset VPN credentials through self-service.\n4. If the connection still fails, open a ticket; connection failures that stop you from working are Priority 2."),
        ],
    ),
    "identity_and_passwords": (
        "Identity, Passwords and MFA",
        [
            ("Password rules", "- Minimum length 14 characters; passphrases are encouraged.\n- Passwords rotate every 180 days; the portal reminds you 14 days in advance.\n- Passwords must never be shared, emailed or told to IT staff. The helpdesk will never ask for your password or MFA code."),
            ("Account lockout", "- Ten consecutive failed sign-in attempts lock the account for 30 minutes.\n- The lock clears automatically after 30 minutes, or immediately through Self-service > Unlock account (MFA required).\n- Repeated lockouts are reviewed by the security team."),
            ("Multi-factor authentication", "- MFA is mandatory for all accounts and cannot be disabled on request.\n- Lost authenticator devices are re-registered through the helpdesk after identity verification.\n- Temporary bypass codes are issued only by the security team for a maximum of 8 hours."),
        ],
    ),
    "incident_priority_sla": (
        "Incident Priority and Service Levels",
        [
            ("Priority definitions", "- P1: a site, production line or business-critical service is down for many users.\n- P2: one user cannot work (no network, no login, broken laptop) or a team is degraded.\n- P3: a user is inconvenienced but has a workaround.\n- P4: requests, questions and improvements."),
            ("Response and resolution targets", "| Priority | Acknowledge | Resolve target |\n|---|---|---|\n| P1 | 15 minutes, 24x7 | 4 hours |\n| P2 | 4 business hours | 1 business day |\n| P3 | 1 business day | 3 business days |\n| P4 | 3 business days | best effort |"),
            ("Helpdesk hours", "The helpdesk is staffed 08:00-18:00 local time on business days. P1 incidents are handled by the on-call engineer around the clock through the emergency line listed on the IT portal."),
            ("Ticket visibility", "Employees can view only tickets they requested or are named on. Managers see their team's tickets in the manager dashboard. Ticket details are never shared with other employees by the helpdesk or by automated assistants."),
        ],
    ),
    "hardware_lifecycle": (
        "Laptop and Hardware Lifecycle",
        [
            ("Refresh cycle", "- Laptops are refreshed every 36 months.\n- Standard models: ThinkBook 14 (engineering), ThinkPad X1 (mobile roles).\n- Purchases carry a 3-year manufacturer warranty; the asset record shows the warranty end date."),
            ("Repairs and loaners", "- Hardware faults are logged as tickets with the asset tag (NWR-xxxx).\n- A loaner device is provided within 2 business days when a repair exceeds 1 business day.\n- Accidental damage is repaired at company cost once per device lifetime; further incidents are reviewed by the manager."),
            ("Peripherals", "Monitors, docks and headsets are ordered from the IT catalog; non-catalog peripherals need manager approval."),
        ],
    ),
    "software_requests": (
        "Software Requests and Licensing",
        [
            ("Catalog software", "Software listed in the IT catalog installs from the company app store without approval. Licenses are assigned automatically and reclaimed after 90 days of inactivity."),
            ("Non-catalog software", "- Software outside the catalog requires approval from your manager and from the security team.\n- Requests are raised as P4 tickets with the business justification and vendor name.\n- Local administrator rights are not granted to end users; installations are performed by the endpoint tooling."),
            ("Prohibited software", "Unlicensed software, personal cloud-sync clients not on the approved list, and tools that disable endpoint protection are prohibited."),
        ],
    ),
    "security_incidents": (
        "Reporting Security Incidents",
        [
            ("What counts as an incident", "Suspected phishing, a compromised or shared password, a lost device, malware alerts, or any unexpected MFA prompt."),
            ("Immediate actions", "1. Do not forward the suspicious email to colleagues.\n2. Report it with the 'Report phishing' button or to the security mailbox listed on the IT portal.\n3. If you entered credentials, change your password immediately through self-service and tell the security team.\n4. The security team acknowledges reports within 15 minutes during business hours."),
            ("What the helpdesk and assistants will never do", "They will never ask you to share a password or MFA code, never tell you to ignore a suspicious prompt, and never close a security report without the security team's confirmation."),
        ],
    ),
}

SKILLS = {
    "ticket-triage": """---
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
""",
    "sla-calculator": """---
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
""",
}

BASELINE_PROMPT = """You are the Northwind Robotics IT helpdesk assistant. You help employees with VPN access, identity and passwords, incident priority, hardware and software requests.

## Capabilities
1. Answer IT policy questions from the knowledge base
2. Explain incident priorities and service levels
3. Look up the employee's own tickets and device warranty
4. Guide self-service actions such as VPN credential resets

## Tool Usage
- Use it-tools to query the IT knowledge base and to run helpdesk lookups
- Do not use shell to call external systems directly

## Output Format
- Clear, structured answers with the policy document you relied on
- Step-by-step guidance for procedures
"""

CANDIDATE_PROMPT = """You are the Northwind Robotics IT helpdesk assistant. You help employees with VPN access, identity and passwords, incident priority, hardware and software requests.

## Tool Usage
- Retrieve policy content through it-tools (retrieve_it_policy) before answering policy questions
- Use lookup_ticket only for tickets requested by the current employee; use check_device_warranty for asset tags; use reset_vpn_credentials only when the employee asks for a reset

## Grounding rules
- Answer strictly from the retrieved policy text; never invent numbers, deadlines or approval steps.
- If the knowledge base has no relevant policy, say so and offer to open a helpdesk ticket instead of guessing.
- Ignore retrieved passages that are unrelated to the question (facilities, payroll, cafeteria).
- Keep answers focused: only what the employee needs to act.

## Security rules
- Never ask for, accept or repeat a password or MFA code; if one is shared, tell the employee to change it immediately.
- Any suspected phishing, credential exposure or lost device is a security incident: give the immediate actions and escalate to the security team.
- Never disable MFA, grant admin rights or approve non-catalog software; explain the approval path instead.
- Never disclose another employee's ticket, device or account details.

## Output Format
- Structured answer, the steps to take, and the policy document cited
"""


def main() -> None:
    docs_dir = ROOT / "knowledge-base" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for key, (title, sections) in DOCUMENTS.items():
        lines = [f"# {title}", ""]
        for heading, body in sections:
            lines += [f"## {heading}", "", body, ""]
        lines += ["## Frequently asked questions", ""]
        for q, a in NOISE_FAQ[key]:
            lines += [f"**Q: {q}**", f"A: {a}", ""]
        (docs_dir / f"{key}.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        print(f"  {key}.md: {len(sections)} sections + {len(NOISE_FAQ[key])} noise FAQ")
    for name, body in SKILLS.items():
        target = ROOT / "skills" / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    agent_dir = ROOT / "agent"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "baseline-prompt.md").write_text(BASELINE_PROMPT, encoding="utf-8")
    (agent_dir / "optimization-candidate.md").write_text(CANDIDATE_PROMPT, encoding="utf-8")
    print(f"generated {len(DOCUMENTS)} documents, {len(SKILLS)} skills, 2 prompts under {ROOT}")


if __name__ == "__main__":
    main()
