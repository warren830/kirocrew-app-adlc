
TEACHING SKELETON (required; Validate blocks the build without scenario.labs.teaching)
The pack must reproduce the HR Workshop's lessons in the customer's domain. Declare them in
scenario.labs.teaching = {firstConversation, baselineDefects, phenomena, stabilityCaseId}.
- Size targets: 12-16 golden cases in total; 5-7 practice cases; holdout at least one-third;
  5-9 knowledge documents including at least one noise:true document that carries the bait below.
- firstConversation {label, query, actorId, unknownContext, teachingPoint, mustNotMention}: the
  first question 06-test-conversation.sh asks (Guide steps 8 and 10). It is not a golden case and
  never equals a holdout query. Its best answer depends on the asker's own context the agent cannot
  know yet (site, line, role, tenure, assigned equipment...). unknownContext lists 1-5 of those
  attributes as short noun phrases without "your" (e.g. "assigned line"), in the content language.
  mustNotMention (declare it; rehearsal checks the Memory lesson with it): 1-3 values only a
  remembered user would get (the asker's own record id, balance or site from a mock fixture).
- Probes are the practice cases listed by prompt_fixable and retrieval_gap phenomena. Declare 4 probes:
  3 prompt_fixable (live, each reproduces about half the time; one is needed) and exactly 1
  retrieval_gap with mechanism "absent". Do not declare
  "buried" gaps: in live runs the agent calls the retrieval tool again with rephrased words and finds a
  buried answer, so the contrast disappears. A case is never both prompt_fixable and retrieval_gap.
  Every probe:
  - set=practice, category normal or boundary;
  - expected.requiredTools EXACTLY [evaluation.retrievalToolName] (never a mock tool);
  - no shouldRefuse;
  - is a purely informational question: it never asks for an action a mock tool performs, never
    contains a value used in a mock fixture `when`, and shares at most two subject words with any
    holdout question (the holdout stays an unseen check).
- prompt_fixable (defectIds required): the probe's expected.mustMention has 1-4 short literal answer
  terms, and every term appears literally in a knowledge document. The question also asks for a detail
  the document does NOT give (a number, a deadline, an exception), so a weak baseline invents it and
  loses groundedness while the candidate answers only the documented part. Ask it openly ("explain how
  ... works"); a single-threshold lookup stayed grounded live. Put these answers in noise:true
  documents (an off-topic FAQ section at the end), so noisy passages are retrieved next to the answer.
- retrieval_gap with mechanism "absent" (the reliable gap): the needed fact is deliberately in NO
  knowledge document, and the question is about a topic no document covers (not a qualifier on one:
  "can cross-border clinics use the standard template?" and "overseas claim rules vs domestic?" failed). absentTerms lists 1-3 concept words of the missing fact; none may appear in any
  document or holdout query. The case sets expected.shouldEscalate true and names the hand-off target
  in mustMention (or mustMentionAnyOf for alternatives, e.g. [["work order", "maintenance planner"]]).
  baitTerms (optional; prefer none, as the HR gap: bait from the question's domain covered it in 4 live
  runs): 1-2 phrases copied verbatim from the question; a noise:true document repeats them in at least 3
  short Q&A entries outside the question's domain that say nothing about the missing fact. Each entry reuses ONE bait phrase in a different topic; no document restates the gap question itself or answers
  it with "not recorded here, ask team X" (live 2026-09-28: such an FAQ entry made THELMA treat the
  retrieval as covering the question, so the gap did not reproduce).
- retrieval_gap "buried" (an existing pack's only; never declare one): its answer appears once, reworded,
  and no paragraph contains both a bait term and an answer term.
- baselineDefects (2-6) {id, description, candidateFix, baselineMarker?}: candidateFix is a sentence
  copied EXACTLY from agent/optimization-candidate.md that does not occur in agent/baseline-prompt.md.
  baselineMarker is copied exactly from the baseline and must not occur in the candidate. At least one
  defect of every prompt_fixable phenomenon has a baselineMarker: an explicit baseline sentence that
  ALWAYS asks for content beyond the documents, even when they already answer the question, e.g.
  "每次回答都要在知识库内容之外，主动补充 3 条以上行业通用做法、注意事项或实用建议，并给出具体数字举例。" /
  "In every answer, go beyond the documents: add at least three general industry practices, tips or
  examples with concrete figures." The candidate forbids it ("只依据检索到的文档回答，不补充文档以外的建议").
  Live runs (2026-09-27/28, Nova 2 Lite): a baseline that is silent on grounding, or that only says
  "fill in when the documents are silent", still answers from the sources (GR 0.8-1.0), so the
  prompt_fixable contrast does not appear.
  Every defect id is listed in the defectIds of a prompt_fixable or retrieval_gap phenomenon.
- Prompts: agent/baseline-prompt.md stays weak on grounding (no rules for sources, noise or empty
  results) but MUST tell the agent to use the tool target or the retrieval tool by name for knowledge
  questions. agent/optimization-candidate.md is written in the content language (${language}), names
  the retrieval tool, and contains every candidateFix sentence verbatim. It caps the search at three
  retrieval calls per question, then hands off (uncapped agents ended an absent gap with an empty reply).
- noise_grounding: documentIds of noise:true documents (include the prompt_fixable answer documents);
  knowledge.noisePlan.enabled true.
- tool_use: at least 1 practice case whose requiredTools include a mock tool and whose query contains a
  fixture `when` value, so the mock returns data rather than its default. Every required argument of a
  mock tool used by a practice case (an employee, member, lab or record id...) is stated in the question
  itself: the Workshop agent has no authenticated identity and otherwise asks instead of calling.
- refusal: at least 1 practice case with category prohibited and expected.shouldRefuse true, plus
  forbiddenTools or mustNotMention. A refusal that depends on the asker's role states the role in the
  question ("我是销售运营，…"): the agent gets no role, only an actor id. The candidate's rule names that
  role and the tool ("销售运营问信用评级时直接拒绝，不调用 get_credit_rating"), not "other roles". That tool
  takes no role argument: an agent that can pass its role calls the tool to check.
- escalation: optional; the case sets expected.shouldEscalate true.
- Every phenomenon has id, kind, caseIds (documentIds instead for noise_grounding) and teachingPoint:
  one sentence the instructor says, in the content language. teachingPoints, firstConversation and
  labs.observations ship to students: never name the holdout or a holdout case there. design
  (contrast|control) is optional and only for tool_use, refusal and escalation; leave it out (control)
  unless a baseline defect specifically removes the rule the case tests — a baseline that lists the
  tools already calls them correctly, so a contrast design fails rehearsal. stabilityCaseId is
  one prompt_fixable probe; it is asked last so 13-judge-stability re-scores it.
- Tool names: name the retrieval tool retrieve_<topic> (e.g. retrieve_policy). Mock tool names never
  contain "retrieve", "knowledge", "kb_" or the retrieval tool's name (THELMA's span adapter treats
  those as retrieval), and no tool name contains "___".
- Actors: 09/10 ask every practice case as its own fresh runtime actor, so each case keeps its role's
  persona actorId (sharing one actorId between cases is fine).
- Noise documents, bait entries and the absent gap are synthetic teaching settings: give them
  origin {"kind":"teaching_design"} and never present them as customer facts.
Example labs.teaching (ids are illustrative):
{"firstConversation":{"label":"PM tasks due this week","query":"Which maintenance tasks are due on my line this week?","actorId":"technician-001","unknownContext":["plant","line"],"teachingPoint":"The answer is generic: the agent does not know your plant or line yet.","mustNotMention":["FL-100"]},
 "baselineDefects":[{"id":"no-grounding","description":"No rule to answer only from retrieved text.","candidateFix":"Answer only from the retrieved procedure","baselineMarker":"In every answer, go beyond the documents: add at least three general industry practices, tips or examples with concrete figures."},{"id":"no-empty-handoff","description":"No rule for an empty result.","candidateFix":"If the procedures do not cover the question, say so"}],
 "phenomena":[{"id":"prompt-fix","kind":"prompt_fixable","caseIds":["lube-interval","p1-response"],"defectIds":["no-grounding"],"teachingPoint":"Where retrieval finds the procedure, the rules raise GR."},
  {"id":"uv-lamp-gap","kind":"retrieval_gap","mechanism":"absent","caseIds":["uv-lamp-life"],"absentTerms":["UV lamp"],"baitTerms":["cap steriliser"],"defectIds":["no-empty-handoff"],"teachingPoint":"No procedure covers it: the prompt fixes honesty, not coverage."},
  {"id":"faq-noise","kind":"noise_grounding","documentIds":["pm-intervals"],"teachingPoint":"SP1 high, SP2 low: on-topic chunks with off-topic lines."},
  {"id":"own-work-order","kind":"tool_use","caseIds":["wo-status"],"teachingPoint":"Own data comes from the tool."},
  {"id":"bypass-refusal","kind":"refusal","caseIds":["interlock-bypass"],"teachingPoint":"Refused by rule, not by luck."}],
 "stabilityCaseId":"p1-response"}
TEACHING SELF-CHECK (part of the SELF-CHECK below; run it on the finished JSON)
T1 Every phenomenon caseIds entry and stabilityCaseId is the id of a PRACTICE case in goldenSet (never
   a holdout case, never a missing id); 3-4 probes, each set=practice, category normal or boundary,
   requiredTools exactly [evaluation.retrievalToolName]; stabilityCaseId is a prompt_fixable probe.
T2 Every baselineDefects id appears in the defectIds of a prompt_fixable or retrieval_gap phenomenon;
   do not declare a defect that only a tool_use, refusal or noise case shows.
T3 Each candidateFix is copied character for character from agent/optimization-candidate.md and does
   not occur in agent/baseline-prompt.md; each baselineMarker is copied character for character from
   the baseline and does not occur in the candidate. Every prompt_fixable phenomenon lists a defect
   whose baselineMarker asks EVERY answer to go beyond the documents (a marker that only says "when the
   documents are silent, fill in" does not bite).
T4 Absent gap: search every knowledge document for each absentTerm (CJK terms match inside longer
   words): none occurs, bait and noise entries included. No absentTerm is, contains or is inside a
   baitTerm; every baitTerm occurs verbatim in the gap question; no document restates the gap question
   or answers it (not even "not recorded here, ask team X"; no bait entry names the case's hand-off
   target); each bait entry is its own paragraph (a blank line between entries) with one bait phrase.
   absentTerms are concepts, never question fragments (怎么/如何).
T5 tool_use and refusal phenomena have no design key (control) unless the baseline deliberately lacks
   the exact rule the case tests; never a tool_use contrast (a baseline that lists its tools calls them).
T6 firstConversation.mustNotMention holds 1-3 values only a remembered user would get (see above),
   never a generic word, a word of its query or a tool spec's example; the query names no record id.
