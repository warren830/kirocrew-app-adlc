"""The human-readable text of rehearsal.py in the pack language (``zh-CN`` or ``en``).

``STRINGS[lang][key]`` for ``lang`` in :data:`LANGUAGES`; both languages carry exactly the same keys and
the same ``str.format`` placeholders (a test pins it). rehearsal.py writes no prose of its own: every
case reason, verdict reason, remediation action / because, advisory note, warning, readiness blocker text
and the scope warning is a template here. The machine codes (verdicts, statuses, reason codes, hint
codes, asset kinds) never change with the language.

The engine builds language-independent :class:`Text` values (:class:`Msg`, :class:`Join`,
:class:`Given`) and :func:`localize` renders them at the end: a ``Text`` stored under key ``k`` becomes
``k`` in the pack language plus ``kEn`` in English, so an English reader (and the Kiro repair contract)
always has the original wording next to the localized one.

Chinese keeps the technical tokens untranslated: GR / SP2 / SQC / THELMA / L1 / Mind the Goal, file
paths, scenario paths, ids, field names and the kind vocabulary (prompt_fixable, retrieval_gap, ...).
"""
from __future__ import annotations

import string
from typing import Any, Iterable, Mapping

LANGUAGES: tuple[str, ...] = ("en", "zh-CN")


def lang_key(language: Any) -> str:
    """The text language of a pack: ``zh`` / ``zh-*`` → ``zh-CN``, every other value → ``en``."""
    if isinstance(language, str) and language.strip().lower().replace("_", "-").split("-")[0] == "zh":
        return "zh-CN"
    return "en"


STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # -- building blocks ----------------------------------------------------------------------
        "sep.list": ", ",
        "sep.clause": "; ",
        "value.none": "n/a",
        "run.baseline": "baseline",
        "run.optimized": "optimized",
        "retrieval_tool": "the retrieval tool",
        "because.join": "{reason}; {note}",
        # -- case verdict reasons (teaching_contrast; also report.teachingContrast) ------------------
        "case.NOISE_BAND_MISSING": "no judge noise band (evaluation.noiseBand or 13-judge-stability.sh)",
        "case.JUDGE_TOO_NOISY": "judge noise band {band} > {max}: THELMA cannot separate the runs",
        "case.NOT_SCORED": "no THELMA score for this probe in the {run} run",
        "case.PF_BASELINE_ALREADY_PASSES": "baseline already grounded (GR {gr} >= {bar}): the declared defects did not bite",
        "case.PF_METRICS_MISSING": "THELMA returned no SQC for this probe",
        "case.PF_RETRIEVAL_FAILED": "sources did not cover the question (max SQC {sqc} < {bar}): a retrieval problem, not prompt-fixable",
        "case.PF_NO_GAIN": "GR change {delta} is within max(noise band, {min}) = {bar}",
        "case.PF_REPRODUCED": "GR {baseline} -> {optimized} (+{delta} > {bar}) with the sources covering the question",
        "case.PF_OPTIMIZED_NOT_PASS": "the optimized answer improved but THELMA still labels it {label}",
        "case.RG_METRICS_MISSING": "THELMA returned neither SP2 nor SQC in the {run} run",
        "case.RG_RETRIEVAL_OK": ("retrieval found the answer in the {run} run (SP2 {sp2}, SQC {sqc}; "
                                 "a gap needs SP2 <= {sp2max} or SQC < {sqcmax})"),
        "case.RG_OPTIMIZED_RESOLVED": "retrieval failed in both runs but the optimized answer is labelled {label}, not Fail",
        "case.RG_REPRODUCED.buried": "retrieval failed in both runs and the optimized answer stays Fail: fix retrieval, not the prompt",
        "case.RG_REPRODUCED.absent": "retrieval finds nothing in both runs (the GR label is ignored for an absent answer)",
        "case.RG_REPRODUCED.absent_admitted": (
            "retrieval finds nothing in both runs (the GR label is ignored for an absent answer); "
            "the optimized agent admits it and hands off: the prompt fixed honesty, not coverage"),
        "case.L1_NO_FOCUS_CHECK": "the case declares none of the {kind} checks ({checks})",
        "case.L1_MISSING": "no L1 verdict for this case in the {run} run",
        "case.L1_ERROR": "L1 reported an error for this case in the {run} run",
        "case.L1_UNRESOLVED": "L1 deferred the {run} verdict to Mind the Goal, which gave none",
        "case.L1_BASELINE_ALREADY_PASSES": "contrast design: the baseline already passes the focus checks",
        "case.L1_OPTIMIZED_FAILS": "the optimized run fails the focus checks",
        "case.L1_EMPTY_RESPONSE": ("the optimized reply is empty (L1 response: fail): {checks} failed for lack of a reply, whatever "
                                   "Mind the Goal made of the trace"),
        "case.L1_CONTRAST_REPRODUCED": "the baseline fails and the optimized run passes the focus checks",
        "case.L1_CONTROL_HELD": "the optimized run passes the focus checks",
        "case.CASE_NOT_PRACTICE": "the case is not a practice case of this build",
        "case.UNSUPPORTED_KIND": "unknown phenomenon kind {kind}",
        "advisory.noise_note": "high SP1 with low SP2 in 09 reads as noisy sources; advisory, never decides the contrast",
        "contrast.readiness": "one run only: readiness for class is decided by rehearsal (run/rehearsal.json), not by this report",
        # -- the class verdict -----------------------------------------------------------------------
        "verdict.TEACHING_UNDECLARED": "the build declares no labs.teaching block",
        "verdict.PARITY_MISSING": "no phenomenon of the decisive kind(s): {groups}",
        "verdict.RUN_INCOMPLETE": "no complete run of this release yet",
        "verdict.CONTRASTS_REPRODUCED": "every decisive teaching contrast reproduced in the latest complete run",
        "verdict.PHENOMENON_NOT_REPRODUCED": "a decisive teaching contrast did not reproduce in the latest complete run",
        "verdict.PHENOMENON_INSUFFICIENT": "the latest complete run cannot decide every teaching contrast",
        # -- readiness blockers (readiness.blockerTexts) ---------------------------------------------
        "blocker.VERDICT_NOT_READY": "a decisive teaching contrast did not reproduce",
        "blocker.VERDICT_INSUFFICIENT_EVIDENCE": "the evidence cannot decide every teaching contrast",
        "blocker.REPORT_INCOMPLETE": "the current run's report is incomplete",
        "blocker.GUIDES_MISSING": "the student or instructor guide is not built or not verified",
        "blocker.SCENARIO_NOT_BUILD_SNAPSHOT": "a scenario file was judged, not the build snapshot",
        "blocker.RELEASE_NOT_VERIFIED": "no release manifest confirms the run's release",
        # -- remediation: actions ----------------------------------------------------------------------
        "fix.PF_BASELINE_ALREADY_PASSES.baseline": (
            "Weaken the baseline prompt so the declared defects bite: remove any grounding, citation or "
            "'answer only from the retrieved text' rule it carries, and add a baselineMarker sentence that asks EVERY answer "
            "to go beyond the documents (for example: add three or more general industry practices or tips with concrete "
            "figures); keep the candidate's rule that forbids content outside the documents. A baseline that only says "
            "'fill in when the documents are silent' still answers from the sources."),
        "fix.PF_BASELINE_ALREADY_PASSES.teaching": "Or take {case} out of this prompt_fixable phenomenon if the baseline is meant to answer it well.",
        "fix.PF_BASELINE_ALREADY_PASSES.query": (
            "The baseline already carries the marker ('{marker}') and still answered from the sources: it kept the added "
            "advice apart from the documented answer. Make {case} an open question (explain how it works, what applies "
            "and why) whose documented answer has several specifics, so the baseline mixes invented specifics into the "
            "answer; a narrow lookup (one threshold or number) stays grounded."),
        "fix.PF_NO_GAIN": ("Strengthen the candidate prompt: keep every labs.teaching.baselineDefects[].candidateFix rule "
                           "(grounding, ignore unrelated passages, concise answers) and drop verbose instructions."),
        "fix.PF_RETRIEVAL_FAILED.doc": ("Clean the answer passage so retrieval covers the question: one self-contained section that states "
                                        "the expected answer."),
        "fix.PF_RETRIEVAL_FAILED.teaching": "Or redeclare {case} as a retrieval_gap (mechanism: buried) if retrieval is meant to fail.",
        "fix.NOT_SCORED.query": ("Make the question clearly need the knowledge base (THELMA scores only answers that "
                                 "called {tool}) and keep expected.requiredTools = [{tool}]."),
        "fix.NOT_SCORED.baseline": "Tell the agent to look policy questions up with {tool}: the baseline must still retrieve.",
        "fix.RG_ABSENT_NOT_SCORED": ("Check the 09/10 THELMA block of this case: THELMA may skip a trace whose retrieval returned nothing; "
                                     "rerun from baseline and compare."),
        "fix.METRICS_MISSING": ("THELMA returned no SP2/SQC dimensions: redeploy the add-ons RunStep document "
                                "(sync/cfn/customizer-addons.json) so scores carry metrics, then reset from baseline."),
        "fix.NOISE_BAND_MISSING": ("Run 13-judge-stability.sh (the judge-stability step) or "
                                   "calibrate evaluation.noiseBand.thelma_rag_quality."),
        "fix.JUDGE_TOO_NOISY": ("The judge is too noisy to separate the runs: re-run "
                                "13-judge-stability.sh with more runs and recalibrate evaluation.noiseBand (the Workshop judge model is fixed; "
                                "evaluation.judgeModel does not change it)."),
        "fix.RG_OPTIMIZED_RESOLVED": ("The optimized answer is not labelled Fail although retrieval failed: declare "
                                      "mechanism: absent if the answer is not in the knowledge base (the optimized agent admits the gap)."),
        "fix.RG_RETRIEVAL_OK.absent": "Remove the answer from the knowledge base: no document may state it.",
        "fix.RG_RETRIEVAL_OK.absent_terms": "Remove the answer from the knowledge base: no document may state it ({terms}).",
        "fix.RG_RETRIEVAL_OK.absent_query": (
            "No document states {terms}, yet retrieval covered {case}, and {doc} holds {share} of the question's other "
            "words. If the question is about that documented topic, ask about a topic no document covers instead: a "
            "qualifier on a documented topic (can X use the standard template?) is not a gap."),
        "fix.RG_RETRIEVAL_OK.absent_bait": ("Keep this document's bait lines away from the documented topic the gap "
                                            "question names, so retrieval does not return them as coverage."),
        "fix.RG_RETRIEVAL_OK.buried": ("Make retrieval fail by design: split or remove the answer-bearing passage, or surround it with noisy "
                                       "near-duplicates (noise: true documents carrying the baitTerms)."),
        "fix.RG_REDECLARE": "Or redeclare {case} as prompt_fixable if retrieval is meant to find the answer.",
        "fix.L1_BASELINE_ALREADY_PASSES.baseline": ("The baseline already passes the {kind} checks: remove the rule from the baseline prompt that the "
                                                    "contrast is meant to show."),
        "fix.L1_BASELINE_ALREADY_PASSES.teaching": "Or declare design: control on {phenomenon} (only the optimized run is judged).",
        "fix.L1_ROLE_RULE": ("If it does not yet, write the candidate's refusal rule for the role, not for \"other roles\": "
                             "\"{role} asking for this: say they have no access and do not call {tool}\"."),
        "fix.L1_GATED_TOOL_ROLE_ARG": ("Take the caller's role out of {tool}: drop the '{arg}' argument and the fixture branches "
                                       "on it, and the access rule from its description (keep it in the prompts). The agent "
                                       "fills in its own role and calls the tool to check before refusing."),
        "fix.L1_ROLE_NOT_IN_QUERY": ("State the asker's role in the question (for example \"I'm in {role}, ...\"): the agent gets only "
                                    "an actor id, not the role, and calls {tool} to find out."),
        "fix.L1_OPTIMIZED_FAILS": "Add an explicit rule to the candidate prompt: {rule}.",
        "fix.L1_EMPTY_RESPONSE": ("Make the candidate prompt's rule explicit: {rule}, and end every turn with a reply, also when a search "
                                  "finds nothing. Students otherwise see an empty reply."),
        "fix.L1_BASELINE_EMPTY_RESPONSE": ("The contrast counts the empty baseline reply as the missing {kind} behavior, but students see "
                                           "no reply to read: keep the baseline prompt without the rule the contrast shows and make it end "
                                           "every turn with a reply, or declare design: control on {phenomenon}."),
        "rule.tool_use": "call {tools} for this kind of question",
        "rule.case_tool": "the case tool",
        "rule.refusal": "refuse it",
        "rule.refusal_forbidden": "refuse it and never call {tools}",
        "rule.escalation": "hand it off (name the hand-off target)",
        "rule.default": "follow the case expectations",
        "rule.absent_gap": "say the knowledge base does not cover it and hand off",
        "fix.L1_TOOL_FIXTURE": ("Make sure the {tool} fixture answers this case's arguments (an error or empty fixture "
                                "teaches the agent to skip the tool)."),
        "fix.L1_EXPECTED_CHECK": "Check that expected.* of {case} is what the agent should do.",
        "fix.L1_REFUSAL_TERM_SAID": ("The optimized answer refused and called no forbidden tool, but used a word {case} forbids "
                                     "({terms}): a correct refusal also names what it refuses. Forbid only what would leak (a "
                                     "fixture value, or a disclosure phrase ending in 是 / 为 / \"is\"); shouldRefuse already "
                                     "checks the refusal, and a wording phrase in mustMentionAnyOf is matched literally."),
        "fix.L1_MISSING": ("The release's L1 checker gave no verdict for this case: redeploy the add-ons RunStep document, "
                           "check the l1_eval.py lines of 09/10, then reset from baseline."),
        "fix.L1_UNRESOLVED": ("Give the case a decidable assertion (mustMention / mustMentionAnyOf) or declare "
                              "evaluation.l1.escalationMarkers / refusalMarkers so L1 does not defer to Mind the Goal."),
        "fix.L1_NO_FOCUS_CHECK": "Declare the {kind} checks on {case}: one of {checks}.",
        "fix.PHENOMENON_DECLARATION": "Fix the phenomenon declaration: practice cases and the fixed kind vocabulary only.",
        "fix.CONVERSATION_EVIDENCE_MISSING": ("06 left no conversation excerpt (outputs.conversation): "
                                              "redeploy the add-ons RunStep document and rerun the conversation step."),
        "fix.MEMORY_PERSONALIZED": ("The first conversation already knows the "
                                    "user: use a fresh firstConversation.actorId (or clear that actor's memory) before class."),
        "fix.MEMORY_TERM_GIVEN": ("The first answer repeats a value the agent was given (the question or a tool description), "
                                  "not a memory: make firstConversation.mustNotMention a value only the asker's record holds, "
                                  "and describe tool formats without fixture values."),
        "fix.MEMORY_UNCHECKED": ("Declare firstConversation.mustNotMention (a value only a remembered user would get) so rehearsal can check "
                                 "the Memory lesson."),
        "fix.NOISE_NOT_OBSERVED": "Keep the off-topic FAQ lines next to the answer passages so the noisy chunks are retrieved with them.",
        "fix.ABSENT_GAP_EMPTY_ANSWER": ("Cap the search in the candidate prompt at {cap} {tool} calls per question, then {rule}; if the prompt "
                                        "already has this cap, it did not hold: make it the first rule. Students otherwise see an empty reply."),
        "fix.EMPTY_REPLY.over_cap": ("The candidate prompt's retrieval cap did not hold, or there is none: make \"at most {cap} {tool} calls "
                                     "per question, then {rule}\" its first rule, and say that when a reworded search also finds nothing, "
                                     "the search is over. Students otherwise see an empty reply."),
        "fix.TEACHING_UNDECLARED": ("Declare labs.teaching (first conversation, baseline defects, phenomena) and rebuild; the contrast "
                                    "cannot be rehearsed without it."),
        "fix.PARITY_MISSING": "Declare at least one {what} phenomenon (the HR workshop teaches it).",
        "parity.l1": "tool_use / refusal / escalation",
        "fix.RUN_INCOMPLETE": ("Run all 15 Guided steps to a complete report (reset from baseline to "
                               "re-run the evaluation), then rehearse again."),
        "fix.GUIDE_MISSING": ("Rebuild the release: every build generates both "
                              "guides, and a guide that is missing or changed after the build (it no longer matches build/checksums.json "
                              "or RELEASE.json) does not count."),
        "fix.SCENARIO_NOT_BUILD_SNAPSHOT": ("Rehearse the project directory, or pass --build-dir with the "
                                            "build the run executed: readiness judges the build snapshot, never a scenario file."),
        "fix.RELEASE_NOT_VERIFIED": ("Rehearse against the build the run executed: its "
                                     "build/release/RELEASE.json must name the run's release."),
        # -- remediation: why (because) ----------------------------------------------------------------
        "why.L1_OPTIMIZED_FAILS": "optimized L1 failed: {checks}",
        "why.docs_unavailable": "the built knowledge documents are not available to search",
        "why.not_golden": "'{case}' is not a golden case of this build",
        "why.no_answer_doc": "no knowledge document contains the expected answer terms",
        "why.no_absent_doc": "no knowledge document states an absentTerm or carries a baitTerm",
        "why.CONVERSATION_EVIDENCE_MISSING": "the Memory lesson cannot be checked without the first answer",
        "why.MEMORY_PERSONALIZED": "the 06 answer mentions {terms}",
        "why.MEMORY_TERM_GIVEN": "the 06 answer mentions {terms}, which the agent also reads in {where}",
        "why.MEMORY_UNCHECKED": "no memory marker is declared",
        "why.EMPTY_REPLY": "the {run} agent returned an empty reply to {case} (L1 response: fail)",
        "why.EMPTY_REPLY.calls": "the {run} agent called {tool} at least {count} times on {case}, then returned an empty reply (L1 response: fail)",
        "why.EMPTY_REPLY.limit": ("the {run} agent called {tool} at least {count} times on {case}, which reaches the harness iteration limit "
                                  "(--max-iterations {limit} in 04-deploy.sh; no pack setting changes it): the turn most likely ended at the "
                                  "limit, before any reply (L1 response: fail)"),
        "why.NOISE_NOT_OBSERVED": "no scored probe shows SP1 above SP2",
        "why.PARITY_MISSING": "group {group} is missing",
        "why.RUN_INCOMPLETE": "the current run is incomplete",
        "why.RUN_INCOMPLETE.missing": "the current run is incomplete: missing {missing}",
        "why.GUIDE_MISSING": "not built or not verified: {paths}",
        "why.SCENARIO_NOT_BUILD_SNAPSHOT": "the scenario was read from a scenario file, not from build/instructor/scenario-snapshot.*",
        "why.RELEASE_NOT_VERIFIED": "no release manifest confirms that the build holds release {release}",
        # -- warnings ------------------------------------------------------------------------------------
        "warn.DECISIVE_RUN_ARCHIVED": "the current run has no complete report; the verdict comes from {source}",
        "warn.VERDICT_MIXED": "complete runs of this release disagree on the class verdict; the latest decides",
        "warn.PHENOMENON_MIXED": "{phenomenon} reproduced in {reproduced} of {runs} complete runs; run again before class",
        "warn.ABSENT_GAP_NOT_ADMITTED": "the optimized answer does not admit the gap or hand off (the lesson says it does)",
        "warn.ABSENT_GAP_NO_ANSWER": ("the optimized agent returned an empty reply: students see no answer, so the lesson "
                                      "(admit the gap, hand off) is not shown, whatever the agent did with its tools"),
        "warn.MTG_REFUSAL_CONFLICT": ("L1 passes the optimized answer but Mind the Goal labels the goal Fail: the judge may read "
                                      "a correct refusal or hand-off as a failure; check agent.prohibitedBehaviors / handoffConditions"),
        "scope_warning": ("Rehearsal verdicts cover the teaching design in this rehearsal environment; single-run LLM results can vary "
                          "in class. Holdout cases are not executed (instructor-only; see the instructor guide). Not a production approval."),
    },
    "zh-CN": {
        # -- building blocks ----------------------------------------------------------------------
        "sep.list": "、",
        "sep.clause": "；",
        "value.none": "缺失",
        "run.baseline": "基线",
        "run.optimized": "优化",
        "retrieval_tool": "检索工具",
        "because.join": "{reason}；{note}",
        # -- case verdict reasons (teaching_contrast; also report.teachingContrast) ------------------
        "case.NOISE_BAND_MISSING": "没有裁判噪声带（evaluation.noiseBand 或 13-judge-stability.sh）",
        "case.JUDGE_TOO_NOISY": "裁判噪声带 {band} > {max}：THELMA 区分不了两轮结果",
        "case.NOT_SCORED": "{run}轮没有这道探针题的 THELMA 分数",
        "case.PF_BASELINE_ALREADY_PASSES": "基线回答已经有据（GR {gr} ≥ {bar}）：声明的基线缺陷没起作用",
        "case.PF_METRICS_MISSING": "THELMA 没有返回这道探针题的 SQC",
        "case.PF_RETRIEVAL_FAILED": "检索来源没覆盖问题（SQC 最高 {sqc} < {bar}）：这是检索问题，改提示词修不好",
        "case.PF_NO_GAIN": "GR 变化 {delta} 没超过 max(裁判噪声带, {min}) = {bar}",
        "case.PF_REPRODUCED": "GR {baseline} → {optimized}（+{delta} > {bar}），且检索来源覆盖了问题",
        "case.PF_OPTIMIZED_NOT_PASS": "优化后的回答有提升，但 THELMA 仍判为 {label}",
        "case.RG_METRICS_MISSING": "{run}轮 THELMA 既没返回 SP2，也没返回 SQC",
        "case.RG_RETRIEVAL_OK": "{run}轮检索找到了答案（SP2 {sp2}，SQC {sqc}；构成缺口要求 SP2 ≤ {sp2max} 或 SQC < {sqcmax}）",
        "case.RG_OPTIMIZED_RESOLVED": "两轮检索都失败，但优化后的回答被判为 {label}，不是 Fail",
        "case.RG_REPRODUCED.buried": "两轮检索都失败，优化后的回答仍是 Fail：该修的是检索，不是提示词",
        "case.RG_REPRODUCED.absent": "两轮检索都找不到答案（答案本来就不在知识库里，不看 GR 标签）",
        "case.RG_REPRODUCED.absent_admitted": (
            "两轮检索都找不到答案（答案本来就不在知识库里，不看 GR 标签）；"
            "优化后的 Agent 承认知识库里没有答案并转交处理：提示词修好的是诚实，不是覆盖面"),
        "case.L1_NO_FOCUS_CHECK": "这道题没有声明任何 {kind} 判据（{checks}）",
        "case.L1_MISSING": "{run}轮没有这道题的 L1 结论",
        "case.L1_ERROR": "{run}轮 L1 判这道题时出错",
        "case.L1_UNRESOLVED": "L1 把{run}轮的结论交给 Mind the Goal，但 Mind the Goal 没给出结论",
        "case.L1_BASELINE_ALREADY_PASSES": "对比设计（contrast）：基线已经通过关键判据",
        "case.L1_OPTIMIZED_FAILS": "优化轮没通过关键判据",
        "case.L1_EMPTY_RESPONSE": "优化轮的回答是空的（L1 response: fail）：{checks} 没有回答可读，算作没通过，不管 Mind the Goal 怎么判这条 trace",
        "case.L1_CONTRAST_REPRODUCED": "基线没通过关键判据，优化轮通过了",
        "case.L1_CONTROL_HELD": "优化轮通过了关键判据",
        "case.CASE_NOT_PRACTICE": "这道题不是本次构建的练习题",
        "case.UNSUPPORTED_KIND": "未知的教学现象类型 {kind}",
        "advisory.noise_note": "09 里 SP1 高而 SP2 低，说明检索来源带噪声；仅供参考，不决定对比结论",
        "contrast.readiness": "只看这一轮：能否上课由彩排（run/rehearsal.json）判定，不由这份报告判定",
        # -- the class verdict -----------------------------------------------------------------------
        "verdict.TEACHING_UNDECLARED": "这次构建没有声明 labs.teaching",
        "verdict.PARITY_MISSING": "缺少决定性类型的教学现象：{groups}",
        "verdict.RUN_INCOMPLETE": "这个 Release 还没有一次完整运行",
        "verdict.CONTRASTS_REPRODUCED": "最近一次完整运行里，所有决定性教学对比都复现了",
        "verdict.PHENOMENON_NOT_REPRODUCED": "最近一次完整运行里，有决定性教学对比没复现",
        "verdict.PHENOMENON_INSUFFICIENT": "最近一次完整运行的证据不足以判定全部教学对比",
        # -- readiness blockers (readiness.blockerTexts) ---------------------------------------------
        "blocker.VERDICT_NOT_READY": "有决定性教学对比没复现",
        "blocker.VERDICT_INSUFFICIENT_EVIDENCE": "证据不足以判定全部教学对比",
        "blocker.REPORT_INCOMPLETE": "当前运行的报告不完整",
        "blocker.GUIDES_MISSING": "学员手册或导师手册没有生成或没通过校验",
        "blocker.SCENARIO_NOT_BUILD_SNAPSHOT": "判定的是单独的 scenario 文件，不是构建快照",
        "blocker.RELEASE_NOT_VERIFIED": "没有 Release 清单能确认这次构建就是本次运行的 Release",
        # -- remediation: actions ----------------------------------------------------------------------
        "fix.PF_BASELINE_ALREADY_PASSES.baseline": (
            "削弱基线提示词，让声明的缺陷真正起作用：删掉其中所有要求有据、引用来源或“只根据检索内容回答”的规则，"
            "并加一句 baselineMarker，要求每一次回答都超出文档（例如补充三条以上带具体数字的行业通用做法或建议）；"
            "候选提示词里禁止文档外内容的规则保持不变。只写“文档没写时再补充”的基线，仍会照着来源回答。"),
        "fix.PF_BASELINE_ALREADY_PASSES.teaching": "或者：如果基线本来就该答好这道题，把 {case} 移出这个 prompt_fixable 现象。",
        "fix.PF_BASELINE_ALREADY_PASSES.query": "基线已经带着诱发句（“{marker}”），却仍然照着文档回答：它把补充的建议放在了文档答案之外。把 {case} 改成开放式问题（讲清楚怎么做、适用哪些规定、为什么），且文档里的答案包含多处具体细节，让基线把编造的细节混进答案；只问一个阈值或数字的窄问题仍会有据可依。",
        "fix.PF_NO_GAIN": "加强候选提示词：保留 labs.teaching.baselineDefects[].candidateFix 的每条规则（有据回答、忽略无关段落、回答简洁），删掉冗长的说明。",
        "fix.PF_RETRIEVAL_FAILED.doc": "整理答案所在的段落，让检索覆盖这个问题：用一个自成一体的小节写明期望答案。",
        "fix.PF_RETRIEVAL_FAILED.teaching": "或者：如果检索本来就该失败，把 {case} 改声明为 retrieval_gap（mechanism: buried）。",
        "fix.NOT_SCORED.query": "把问题改得明显需要查知识库（THELMA 只给调用了 {tool} 的回答打分），并保留 expected.requiredTools = [{tool}]。",
        "fix.NOT_SCORED.baseline": "在基线提示词里要求 Agent 用 {tool} 查政策类问题：基线也必须检索。",
        "fix.RG_ABSENT_NOT_SCORED": "检查这道题在 09/10 里的 THELMA 输出：检索什么都没返回时，THELMA 可能跳过这条 trace；从基线重跑后对比。",
        "fix.METRICS_MISSING": "THELMA 没有返回 SP2/SQC 维度：重新部署附加栈的 RunStep 文档（sync/cfn/customizer-addons.json），让分数带上各项指标，然后从基线重置。",
        "fix.NOISE_BAND_MISSING": "运行 13-judge-stability.sh（Judge 稳定性这一步），或标定 evaluation.noiseBand.thelma_rag_quality。",
        "fix.JUDGE_TOO_NOISY": ("裁判噪声带太宽，区分不了两轮结果：用更多次数重跑 13-judge-stability.sh，并重新标定 evaluation.noiseBand"
                                "（Workshop 的裁判模型是固定的，改 evaluation.judgeModel 不起作用）。"),
        "fix.RG_OPTIMIZED_RESOLVED": "检索失败了，但优化后的回答没被判为 Fail：如果知识库里本来就没有答案，改声明 mechanism: absent（优化后的 Agent 会承认缺口）。",
        "fix.RG_RETRIEVAL_OK.absent": "把答案从知识库里删掉：任何文档都不能写到它。",
        "fix.RG_RETRIEVAL_OK.absent_terms": "把答案从知识库里删掉：任何文档都不能写到它（{terms}）。",
        "fix.RG_RETRIEVAL_OK.absent_query": "没有文档写到 {terms}，检索却覆盖了 {case}；问题里其余的词有 {share} 出现在 {doc}。如果这道题问的其实是这个文档已有的主题，就改问一个没有任何文档涉及的主题：在已有主题上加一个限定（X 能不能用标准模板？）不构成缺口。",
        "fix.RG_RETRIEVAL_OK.absent_bait": "让这份文档里的诱饵句远离缺口问题所提到的、文档已有的主题，免得检索把它们当成覆盖返回。",
        "fix.RG_RETRIEVAL_OK.buried": "按设计让检索失败：拆开或删掉含答案的段落，或在它周围放相似的干扰内容（标了 noise: true 且带 baitTerms 的文档）。",
        "fix.RG_REDECLARE": "或者：如果检索本来就该找到答案，把 {case} 改声明为 prompt_fixable。",
        "fix.L1_BASELINE_ALREADY_PASSES.baseline": "基线已经通过 {kind} 判据：从基线提示词里删掉这组对比要展示的那条规则。",
        "fix.L1_BASELINE_ALREADY_PASSES.teaching": "或者：给 {phenomenon} 声明 design: control（只判优化轮）。",
        "fix.L1_ROLE_RULE": "如果还没写：候选提示词的拒绝规则要写明角色，而不是笼统的“其他角色”：“{role}问到这类信息时，直接说明无权限，不调用 {tool}”。",
        "fix.L1_GATED_TOOL_ROLE_ARG": "把调用者角色从 {tool} 里拿掉：删掉参数 '{arg}' 和按它区分的 fixture，也把权限规则从工具描述里删掉（留在提示词里）。Agent 会自己填上角色、先调用工具确认，然后才拒绝。",
        "fix.L1_ROLE_NOT_IN_QUERY": "在问题里写明提问者的角色（例如“我是{role}，…”）：Agent 只拿到 actor id，不知道角色，就会调用 {tool} 去查。",
        "fix.L1_OPTIMIZED_FAILS": "在候选提示词里加一条明确的规则：{rule}。",
        "fix.L1_EMPTY_RESPONSE": "把候选提示词里的规则写明确：{rule}，并且每一轮都要给出回答，检索什么都没找到时也一样。否则学员看到的是空回答。",
        "fix.L1_BASELINE_EMPTY_RESPONSE": ("对比把空的基线回答算作缺少 {kind} 行为，但学员看不到可读的回答：基线提示词仍然不写这组对比要展示的那条规则，"
                                           "但要让它每一轮都给出回答；或者给 {phenomenon} 声明 design: control。"),
        "rule.tool_use": "这类问题要调用 {tools}",
        "rule.case_tool": "这道题的工具",
        "rule.refusal": "拒绝这类请求",
        "rule.refusal_forbidden": "拒绝这类请求，并且绝不调用 {tools}",
        "rule.escalation": "这类问题要转交处理（写明转交给谁）",
        "rule.default": "按这道题的期望行为处理",
        "rule.absent_gap": "说明知识库里没有相关内容并转交处理",
        "fix.L1_TOOL_FIXTURE": "确认 {tool} 的 fixture 能响应这道题的参数（fixture 报错或返回空，会让 Agent 学会跳过这个工具）。",
        "fix.L1_EXPECTED_CHECK": "核对 {case} 的 expected.* 是否就是 Agent 应有的做法。",
        "fix.L1_REFUSAL_TERM_SAID": ("优化轮已经拒绝、也没调用被禁止的工具，但说了 {case} 禁止的词（{terms}）：正确的拒绝也会说出它拒绝的是什么。"
                                     "只禁止会泄露的内容（工具 fixture 里的值，或以“是 / 为”结尾的披露说法）；拒绝本身由 shouldRefuse 检查，"
                                     "写进 mustMentionAnyOf 的措辞短语是按字面匹配的。"),
        "fix.L1_MISSING": "Release 里的 L1 检查没给出这道题的结论：重新部署附加栈的 RunStep 文档，检查 09/10 里 l1_eval.py 那几行输出，然后从基线重置。",
        "fix.L1_UNRESOLVED": ("给这道题一个可判定的断言（mustMention / mustMentionAnyOf），或声明 evaluation.l1.escalationMarkers / "
                              "refusalMarkers，让 L1 不必交给 Mind the Goal 判定。"),
        "fix.L1_NO_FOCUS_CHECK": "给 {case} 声明 {kind} 判据：{checks} 中的一项。",
        "fix.PHENOMENON_DECLARATION": "修正教学现象的声明：只能列练习题，类型只能取固定词表。",
        "fix.CONVERSATION_EVIDENCE_MISSING": "06 没有留下对话摘录（outputs.conversation）：重新部署附加栈的 RunStep 文档，再重跑“第一次对话”这一步。",
        "fix.MEMORY_PERSONALIZED": "第一次对话已经认识这个用户：课前换一个新的 firstConversation.actorId（或清掉这个 actor 的记忆）。",
        "fix.MEMORY_TERM_GIVEN": "第一次的回答复述的是 Agent 本来就能看到的值（问题本身或工具描述里的），不是记忆：把 firstConversation.mustNotMention 换成只有提问者自己的记录里才有的值，工具描述写格式、不写 fixture 里的值。",
        "fix.MEMORY_UNCHECKED": "声明 firstConversation.mustNotMention（只有被记住的用户才会得到的值），彩排才能检查 Memory 这一课。",
        "fix.NOISE_NOT_OBSERVED": "把无关的 FAQ 行留在答案段落旁边，让带噪声的 chunk 和答案一起被检索出来。",
        "fix.ABSENT_GAP_EMPTY_ANSWER": ("在候选提示词里限制检索：每个问题最多调用 {tool} {cap} 次，然后{rule}；如果提示词里已经有这条上限，"
                                        "说明它没起作用，把它写成第一条规则。否则学员看到的是空回答。"),
        "fix.EMPTY_REPLY.over_cap": ("候选提示词里的检索上限没起作用（或者根本没写）：把“每个问题最多调用 {tool} {cap} 次，然后{rule}”"
                                     "写成第一条规则，并写明换关键词仍找不到就结束检索。否则学员看到的是空回答。"),
        "fix.TEACHING_UNDECLARED": "声明 labs.teaching（第一次对话、基线缺陷、教学现象）后重新构建；没有它就无法彩排教学对比。",
        "fix.PARITY_MISSING": "至少声明一个 {what} 教学现象（HR 通用版也教这一点）。",
        "parity.l1": "tool_use / refusal / escalation",
        "fix.RUN_INCOMPLETE": "把 15 个 Guided 步骤全部跑完、得到完整报告（要重跑评估就从基线重置），再重新彩排。",
        "fix.GUIDE_MISSING": ("重新构建 Release：每次构建都会生成两份手册；缺失的手册，或构建后被改过、"
                              "与 build/checksums.json 或 RELEASE.json 不符的手册，都不算数。"),
        "fix.SCENARIO_NOT_BUILD_SNAPSHOT": "对项目目录彩排，或用 --build-dir 指定这次运行所用的构建：就绪判定只看构建快照，从不看单独的 scenario 文件。",
        "fix.RELEASE_NOT_VERIFIED": "用这次运行所用的构建来彩排：它的 build/release/RELEASE.json 必须写明本次运行的 Release。",
        # -- remediation: why (because) ----------------------------------------------------------------
        "why.L1_OPTIMIZED_FAILS": "优化轮 L1 没通过：{checks}",
        "why.docs_unavailable": "没有已构建的知识库文档可供查找",
        "why.not_golden": "'{case}' 不是本次构建的黄金集题目",
        "why.no_answer_doc": "没有知识库文档包含期望答案里的关键词",
        "why.no_absent_doc": "没有知识库文档写到 absentTerm，也没有文档带 baitTerm",
        "why.CONVERSATION_EVIDENCE_MISSING": "没有第一次的回答，就检查不了 Memory 这一课",
        "why.MEMORY_PERSONALIZED": "06 的回答提到了 {terms}",
        "why.MEMORY_TERM_GIVEN": "06 的回答提到了 {terms}，而 Agent 在 {where} 里本来就能看到它",
        "why.MEMORY_UNCHECKED": "没有声明记忆标记",
        "why.EMPTY_REPLY": "{run}轮的 Agent 对 {case} 回了一个空回答（L1 response: fail）",
        "why.EMPTY_REPLY.calls": "{run}轮的 Agent 在 {case} 上至少调用了 {count} 次 {tool}，最后回了一个空回答（L1 response: fail）",
        "why.EMPTY_REPLY.limit": ("{run}轮的 Agent 在 {case} 上至少调用了 {count} 次 {tool}，达到了 harness 的迭代上限（04-deploy.sh 里的 "
                                  "--max-iterations {limit}，pack 的任何设置都改不了）：这一轮很可能在上限处结束，没来得及回答（L1 response: fail）"),
        "why.NOISE_NOT_OBSERVED": "没有一道已打分的探针题 SP1 高于 SP2",
        "why.PARITY_MISSING": "缺少 {group} 这一组",
        "why.RUN_INCOMPLETE": "当前运行不完整",
        "why.RUN_INCOMPLETE.missing": "当前运行不完整：缺少 {missing}",
        "why.GUIDE_MISSING": "没有生成或没通过校验：{paths}",
        "why.SCENARIO_NOT_BUILD_SNAPSHOT": "scenario 读自单独的 scenario 文件，不是 build/instructor/scenario-snapshot.*",
        "why.RELEASE_NOT_VERIFIED": "没有 Release 清单能确认这次构建就是 Release {release}",
        # -- warnings ------------------------------------------------------------------------------------
        "warn.DECISIVE_RUN_ARCHIVED": "当前运行没有完整报告；结论来自 {source}",
        "warn.VERDICT_MIXED": "这个 Release 的几次完整运行对上课结论不一致；以最近一次为准",
        "warn.PHENOMENON_MIXED": "{phenomenon} 在 {runs} 次完整运行中复现了 {reproduced} 次；课前再跑一次",
        "warn.ABSENT_GAP_NOT_ADMITTED": "优化后的回答既没承认缺口，也没有转交（这一课要求它这样做）",
        "warn.ABSENT_GAP_NO_ANSWER": "优化后的 Agent 回了一个空回答：学员看不到任何回答，这一课（承认缺口并转交）就没讲出来，不论它用工具做了什么",
        "warn.MTG_REFUSAL_CONFLICT": ("L1 判优化后的回答通过，但 Mind the Goal 判目标 Fail：裁判可能把正确的拒绝或转交当成失败；"
                                      "检查 agent.prohibitedBehaviors / handoffConditions"),
        "scope_warning": ("彩排结论只针对这个彩排环境里的教学设计；LLM 单次运行的结果在课堂上可能不同。"
                          "保留题不会执行（只给导师，见导师手册）。这不是生产上线审批。"),
    },
}


def text(lang: str, key: str, **params: Any) -> str:
    """``STRINGS[lang][key]`` formatted with ``params`` (each rendered in ``lang``); KeyError for an unknown key."""
    return STRINGS[lang_key(lang)][key].format(**{name: render(value, lang) for name, value in params.items()})


def placeholders(template: str) -> frozenset[str]:
    """The ``str.format`` field names of a template."""
    return frozenset(name for _, name, _, _ in string.Formatter().parse(template) if name)


class Text:
    """A human-readable value the engine renders late, once per language (:func:`localize`)."""

    def render(self, lang: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class Msg(Text):
    """One template of :data:`STRINGS` and its parameters (plain values or other :class:`Text`)."""

    __slots__ = ("key", "params")

    def __init__(self, key: str, **params: Any) -> None:
        if key not in STRINGS["en"]:
            raise KeyError(f"unknown rehearsal string {key!r}")
        self.key, self.params = key, params

    def render(self, lang: str) -> str:
        return text(lang, self.key, **self.params)

    def __repr__(self) -> str:
        return f"Msg({self.key!r}, {self.params!r})"


class Join(Text):
    """Items joined with the language's separator (``sep.list`` by default, or ``sep.clause``)."""

    __slots__ = ("items", "sep")

    def __init__(self, items: Iterable[Any], sep: str = "sep.list") -> None:
        self.items, self.sep = tuple(items), sep

    def render(self, lang: str) -> str:
        return text(lang, self.sep).join(render(item, lang) for item in self.items)

    def __repr__(self) -> str:
        return f"Join({self.items!r}, {self.sep!r})"


class Given(Text):
    """Text already rendered by :func:`localize`: ``value`` in the pack language, ``english`` in English."""

    __slots__ = ("value", "english")

    def __init__(self, value: str, english: str | None = None) -> None:
        self.value, self.english = value, value if english is None else english

    def render(self, lang: str) -> str:
        return self.english if lang_key(lang) == "en" else self.value

    @classmethod
    def of(cls, item: Mapping[str, Any], key: str) -> "Given":
        """The localized ``key`` of an already rendered item (``key`` and ``key + 'En'``)."""
        value = str(item.get(key) or "")
        return cls(value, str(item.get(key + "En") or value))

    def __repr__(self) -> str:
        return f"Given({self.value!r}, {self.english!r})"


def render(value: Any, lang: str) -> str:
    """A :class:`Text` in ``lang``; ``None`` → empty; any other value → ``str(value)``."""
    if isinstance(value, Text):
        return value.render(lang)
    return "" if value is None else str(value)


def localize(value: Any, lang: str) -> Any:
    """A copy of JSON-like ``value`` in which every :class:`Text` stored under a key ``k`` becomes ``k`` in
    ``lang`` (the pack language) followed by ``kEn`` in English. Everything else is copied unchanged."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, Text):
                out[key] = item.render(lang)
                out[key + "En"] = item.render("en")
            else:
                out[key] = localize(item, lang)
        return out
    if isinstance(value, list):
        return [localize(item, lang) for item in value]
    return value
