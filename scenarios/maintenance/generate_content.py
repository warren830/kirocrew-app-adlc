"""Generate the fictional Meridian Foods equipment-maintenance pack content deterministically.

Everything here is invented: the company, plants, equipment ids, intervals, part numbers and
people. It is the second non-HR reference pack (plan §Phase 4: "设备维护知识助手") and exists to prove
the engine is scenario-agnostic beyond HR and IT. Run once: ``python3 generate_content.py``.

Teaching devices (declared in scenario.yaml labs.teaching): the FAQ tails of spare_parts and
lockout_tagout repeat the words "cap steriliser" in unrelated questions. They are bait for the
absent retrieval gap cap-steriliser-uv-lamps: the procedures deliberately say nothing about the
steriliser's UV lamps, so retrieval returns look-alike noise and the right answer is "not in the
procedures, raise a work order or ask the maintenance planner". None of these entries may mention
lamps, UV light or a replacement interval.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent

NOISE_FAQ = {
    "preventive_maintenance": [
        ("Where do I get a new pair of safety boots?", "Boots are issued by the plant store on presentation of your badge; one pair per year."),
        ("When does the canteen serve the late shift?", "The canteen serves the late shift between 22:30 and 23:15."),
        ("How do I reserve the training room?", "Training rooms are booked through the plant calendar; ask the shift lead for recurring slots."),
    ],
    "lockout_tagout": [
        ("Is parking allowed behind warehouse B?", "Parking behind warehouse B is reserved for delivery vehicles between 06:00 and 14:00."),
        ("Who approves overtime for weekend shifts?", "Weekend overtime is approved by the production manager, not by maintenance."),
        ("Where is the cap steriliser induction video for new starters?", "Induction videos are on the plant learning portal under Filling."),
    ],
    "work_orders": [
        ("Where is the lost-and-found on site?", "Lost items are kept at the security gate for 30 days."),
        ("Can visitors use the staff canteen?", "Visitors eat in the visitor area unless escorted by their host."),
        ("How do I change my payroll bank account?", "Payroll changes go through the people system, not through maintenance."),
    ],
    "spare_parts": [
        ("Does the plant shuttle run on public holidays?", "The shuttle runs a reduced timetable on public holidays; check the notice board."),
        ("How do I get a visitor badge for a contractor?", "Contractor badges are issued by security with a signed induction form."),
        ("Who keeps the key to the cap steriliser viewing cabinet?", "The shift lead keeps cabinet keys in the key safe at the line office."),
        ("Can we stack empty pallets next to the cap steriliser?", "No. Empty pallets go to the marked zone behind warehouse B."),
    ],
    "fault_codes": [
        ("What time does the plant store close?", "The plant store closes at 17:00 on weekdays; emergency parts follow the on-call procedure."),
        ("Where can I recycle batteries?", "Battery recycling bins are located next to each electrical room."),
    ],
    "food_safety_hygiene": [
        ("How do I book annual leave?", "Annual leave is requested in the people system and approved by your supervisor."),
        ("Is there a bicycle shelter?", "Bicycle shelters are next to gate 2 and gate 4."),
    ],
}

DOCUMENTS = {
    "preventive_maintenance": (
        "Preventive Maintenance Intervals",
        [
            ("Scope", "This procedure defines preventive maintenance (PM) for production equipment at Meridian Foods plants. Intervals are minimum requirements; the CMMS generates the PM work orders automatically."),
            ("Intervals by equipment class", "- Filling line FL-100 series: lubrication every 250 operating hours; full inspection every 2,000 operating hours; changeover seal replacement every 6 months.\n- Conveyor CV-200 series: belt tension check weekly; bearing inspection every 1,000 operating hours; motor thermography every 12 months.\n- Pasteuriser PA-300: gasket inspection monthly; calibration of the holding-tube temperature sensor every 6 months against a certified reference.\n- Compressed-air system CA-400: filter change every 3 months; dryer service annually."),
            ("Overdue PM", "- A PM task is overdue when it passes 110% of its interval.\n- Overdue PM on food-contact equipment (fillers, pasteurisers) requires a quality hold on the line until completed.\n- Overdue PM on non-food-contact equipment is escalated to the maintenance planner and must be scheduled within 5 working days."),
            ("Records", "Every completed PM is recorded in the CMMS with technician id, parts used and measured values. Paper checklists are not an accepted record."),
        ],
    ),
    "lockout_tagout": (
        "Lockout / Tagout (LOTO) Procedure",
        [
            ("Purpose", "LOTO protects technicians from unexpected energisation or start-up during maintenance. It applies to electrical, pneumatic, hydraulic, thermal and gravitational energy."),
            ("Mandatory steps", "1. Notify the line operator and the shift lead.\n2. Shut down the equipment using the normal stop procedure.\n3. Isolate every energy source at the isolation point listed on the equipment energy map.\n4. Apply a personal lock and tag for each technician working on the equipment; one lock per person, no shared locks.\n5. Release stored energy (bleed air, discharge capacitors, lower suspended parts).\n6. Verify zero energy by attempting a start and by measurement.\n7. Remove locks only by the person who applied them, after the area is clear."),
            ("Prohibited", "- Working on energised equipment for speed or convenience is prohibited.\n- Removing another person's lock is prohibited; an abandoned lock is removed only by the maintenance manager following the abandoned-lock procedure with two witnesses.\n- Bypassing interlocks or guards is prohibited even during troubleshooting."),
            ("Authorisation", "Only technicians who hold current LOTO authorisation (renewed every 24 months) may apply locks. Operators may not perform LOTO."),
        ],
    ),
    "work_orders": (
        "Work Order Priorities and Response Targets",
        [
            ("Priority definitions", "- P1 Safety or food-safety risk, or a stopped production line: respond within 15 minutes, 24x7.\n- P2 Reduced output or a single machine down with a workaround: respond within 2 hours during production hours.\n- P3 Defect with no output impact: respond within 2 working days.\n- P4 Improvement request: scheduled in the weekly planning meeting."),
            ("Raising a work order", "Operators raise work orders in the CMMS from the line terminal or the mobile app. A work order needs the equipment id, a description and the observed fault code if any. Verbal requests are not work orders."),
            ("Visibility", "Technicians and operators see work orders for their own plant. Work orders from other plants, and personal data of the reporter, are not shared through the assistant."),
            ("Closure", "A work order is closed by the technician with root cause, parts used and a functional test result. P1 closures require the shift lead's countersignature in the CMMS."),
        ],
    ),
    "spare_parts": (
        "Spare Parts and Critical Stock",
        [
            ("Critical spares", "Critical spares are parts whose absence stops a line for more than 4 hours. They carry a minimum stock level in the plant store and are reordered automatically when stock falls to the minimum."),
            ("Requesting parts", "Parts are requested against a work order. The store issues parts to technicians with a valid work order number; loose issue without a work order is not permitted."),
            ("Lead times", "- Standard stocked parts: same shift.\n- Non-stocked supplier parts: 5 working days.\n- Custom machined parts: 15 working days via the approved workshop."),
            ("Substitutes", "A substitute part may be fitted only if it appears on the approved-equivalents list for that equipment. Food-contact parts must carry a food-grade certificate; no exceptions."),
        ],
    ),
    "fault_codes": (
        "Fault Codes and First Response",
        [
            ("Filling line FL-100", "- E101 Fill weight out of tolerance: stop, verify load cell calibration, check nozzle wear; do not adjust set points beyond the recipe limits.\n- E117 Door interlock open: confirm all guards closed; never bypass the interlock.\n- E140 Servo overtemperature: allow cooling, check the cabinet filter; a second E140 within one shift becomes a P2 work order."),
            ("Conveyor CV-200", "- C205 Belt tracking fault: stop, inspect tracking rollers, re-tension per the tension table.\n- C220 Motor overload: check for jammed product; if the overload repeats, raise a P2 work order."),
            ("Pasteuriser PA-300", "- T301 Holding-tube temperature below set point: product since the last in-spec reading goes on quality hold; raise a P1 work order.\n- T315 Flow diversion valve fault: P1; do not restart production until the valve test passes."),
            ("Escalation", "Any fault code affecting food safety (T301, T315, E101 with contamination risk) is escalated to the quality supervisor immediately."),
        ],
    ),
    "food_safety_hygiene": (
        "Food Safety and Hygiene Rules for Maintenance Work",
        [
            ("Before work", "Maintenance on food-contact equipment requires a hygiene permit from the quality supervisor, clean tools from the food-zone toolbox, and removal of loose items (pens, tools with loose parts) from pockets."),
            ("Lubricants and materials", "Only food-grade (H1) lubricants may be used in food zones. Non-food-grade lubricants in food zones are a food-safety incident."),
            ("After work", "The technician completes the post-maintenance hygiene checklist, and quality releases the equipment before production restarts. Missing tools or parts trigger a search and a hold on the affected product."),
            ("Reporting", "Foreign-material risks, glass or brittle-plastic breakage, and pest sightings are reported to quality immediately, not logged for later."),
        ],
    ),
}

SKILLS = {
    "pm-planner": """# Skill: PM planner

Use this skill when an operator or technician asks whether preventive maintenance is due or overdue.

1. Identify the equipment id and class (FL-100, CV-200, PA-300, CA-400).
2. Retrieve the PM interval for the task from the Preventive Maintenance document; never quote an interval from memory.
3. Compare the hours or months since the last PM (from the CMMS lookup) with the interval.
4. Overdue means past 110% of the interval. Food-contact equipment that is overdue needs a quality hold; say so explicitly.
5. Answer with: due/overdue status, the interval and its source section, and the next action (raise or schedule the work order).
""",
    "fault-first-response": """# Skill: Fault first response

Use this skill when a fault code is reported.

1. Confirm the equipment and the exact code.
2. Retrieve the first-response steps for that code from the Fault Codes document.
3. State the priority (P1/P2/P3) and the response target from the Work Orders document.
4. For food-safety codes (T301, T315, E101 with contamination risk) tell the user to escalate to the quality supervisor immediately and to put affected product on hold.
5. Never suggest bypassing an interlock, adjusting set points beyond recipe limits, or working on energised equipment.
""",
}

BASELINE_PROMPT = """You are the Meridian Foods maintenance assistant. You help plant technicians and operators with preventive maintenance, lockout/tagout, work orders, spare parts, fault codes and hygiene rules.

Answer the user's question helpfully. Use maint-tools (retrieve_maintenance_procedure) to look up procedures, and the other tools when they seem useful.
"""

CANDIDATE_PROMPT = """You are the Meridian Foods maintenance assistant. You help plant technicians and operators with preventive maintenance, lockout/tagout, work orders, spare parts, fault codes and hygiene rules for production equipment.

Rules you must follow:
1. Always retrieve the relevant procedure through maint-tools (retrieve_maintenance_procedure) before answering questions about intervals, priorities, fault codes, parts or hygiene. Quote the interval, priority or step from the retrieved section and name the document.
2. If the retrieved sections do not answer the question, say so and tell the user to raise a work order or ask the maintenance planner. Do not invent intervals, part numbers or response times.
3. Safety first: never suggest bypassing interlocks or guards, working on energised equipment, removing another person's lock, or using non-food-grade lubricant in a food zone. If a request implies any of these, refuse and explain the rule.
4. Food safety: for T301, T315 or a contamination-risk E101, instruct an immediate escalation to the quality supervisor and a product hold.
5. Use lookup_work_order and check_pm_status only for the requesting user's own plant; never disclose other plants' work orders or reporter personal data.
6. Keep answers short: the finding, the rule with its source, and the next action.
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
