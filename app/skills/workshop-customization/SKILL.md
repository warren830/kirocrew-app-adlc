---
description: How to turn a customer conversation into a validated Scenario Pack for the eval-first AgentCore workshop — intake checklist, truth ledger, scenario.yaml shape, provenance and golden-set rules, the fail-closed gate table, and the pre-Workshop sync/rollback runbook. Use whenever the SA works on a Workshop Customizer project.
always: false
---

# Workshop Customization — Scenario Packs for the eval-first workshop

The upstream workshop (pinned in `template-lock.json`) teaches "evaluate first" with an HR
assistant. A **Scenario Pack** replaces the HR business truth with the customer's own scenario
while keeping the workshop mechanics (knowledge base, Gateway tools, prompts, golden set, judge,
labs) identical. One document — `scenario.yaml` — is the single source of truth; everything else
(pack files, release scripts, Lambda fixtures, evaluator inputs) is *derived* from it by the engine.

Roles: the **SA** owns the project and drives the tool. The **customer** confirms facts. The SA
supplies **safe synthetic data** (`sa_synthetic`) wherever real values cannot be shared. On Workshop
day the customer uses their own scenario on the workshop EC2 — nothing is replaced on site.

## 1. Intake checklist (ask in batches of ≤ 6)

| Area | Questions | Lands in |
|---|---|---|
| Audience & purpose | Who asks? What must the agent do for them? | `agent.audience`, `agent.purpose` |
| Scope | 3–6 task types in scope; what is explicitly out of scope | `agent.scope`, `agent.outOfScope` |
| Roles | Roles, what each may see/do; who is a "manager"/"admin" | `agent.roles[]` (id, name, description, permissions) |
| Hand-off | When must the agent escalate to a human/system? | `agent.handoffConditions` |
| Prohibited | What must the agent never do or say? | `agent.prohibitedBehaviors` |
| Facts | 8–20 statements the agent must get right (numbers, deadlines, eligibility, approval paths) | `facts[]` with `criticality` |
| Tools | Which lookups does the agent need? Inputs → outputs, error cases | `tools[]` with `fixtures` |
| Documents | Which policy/FAQ documents exist; which are noisy | `knowledge.documents[]`, `knowledge.noisePlan` |
| Failure picture | What does a *wrong* answer look like here? | golden cases, `labs.observations` |

Never accept credentials, tokens, real personal records or non-shareable figures. Offer a synthetic
stand-in and mark it `sa_synthetic`.

## 2. Truth ledger

Keep and show this table after every intake turn:

`id | statement | criticality (blocking/advisory) | provenance | confirmedBy / confirmationRef | open question`

`provenance` ∈ `customer_confirmed` (needs `confirmedBy`, `confirmedAt`, `confirmationRef`),
`sa_synthetic`, `ai_draft`, `pending`. The last two are drafting states only; they never enter a
formal pack. Kiro always writes `ai_draft` plus an `origin` (`customer_material` citing the uploaded
material ids, `sa_authored`, or `teaching_design` for noise documents, bait and the absent gap); only
the SA records `sa_synthetic` or `customer_confirmed`, in the app. Per-item exceptions exist
(`governance.exceptions[]`, two approvers) and are reported in the provenance report — use them
rarely and say why.

Pack kinds (SPEC D12):

| packKind | Blocking facts, tools, golden cases, documents | Extra rule |
|---|---|---|
| `reference` | `sa_synthetic` allowed (internal / fictional) | — |
| `customer` | `customer_confirmed` (handoff / launch grade, strict) | — |
| `workshop` | `customer_confirmed` or `sa_synthetic` **with an origin** | ≥ 1 customer-confirmed anchor golden case per category (normal / boundary / prohibited) whose basis facts are all `customer_confirmed`; not waivable |

In every kind a noise document labeled `noise: true` + `sa_synthetic` + origin `teaching_design` is a
teaching device and needs no customer confirmation. A `customer_confirmed` whose `confirmedBy` or
`confirmationRef` looks simulated (`simulated`, `e2e_generate`) is refused by the app and the gate.
Item classes (derived, never stored) label what a guide may present as the customer's truth:
`customer-fact`, `derived-setting` (synthetic, from customer materials), `teaching-device`,
`synthetic-setting`, `draft`. RELEASE.json and the provenance report record `provenancePolicy` and the
anchors. Batch review records `sa_synthetic` only and never over a customer confirmation; in a
workshop pack every reviewed item needs an origin, and Kiro never labels a reviewed item for you.
Switch kinds with the audited `POST /projects/{pid}/pack-kind` (acknowledged, a 10–500 character
reason); a change saved in the scenario editor is recorded in `packKindHistory` too.

Record confirmations in the app (02 内容与评估 → 审核 → *确认…* → *记录客户确认* / *记为 SA 合成*, or the
batch review): it writes provenance and evidence into `scenario.yaml`; the SA never edits provenance by
hand. The first draft is generated inside the App: describe the customer scenario (and upload
materials) in 01 场景与课程, then **生成草稿**. The App uses its own restricted KiroCrew agent through the
user's logged-in Kiro CLI, shows the open questions, diff and generated files, and writes nothing until
the SA ticks the review box and clicks **应用并校验**; 02 → **Kiro 按校验结果修复** starts a repair round. This drafting step
uses no Bedrock, model API key, AWS profile or Workshop account. Bedrock/AgentCore belong only to the
later Workshop labs after the validated release has been synced.

## 3. scenario.yaml shape (schemaVersion 1)

```yaml
schemaVersion: 1
id: acme-hr                      # project id, kebab-case
displayName: ACME HR Assistant
description: >
  One paragraph: who it serves, what it does, what it must never do.
packKind: customer               # customer | reference
language: en
namespace:                       # every AWS resource name derives from these
  agentName: acmehr
  toolTargetName: acmehr-tools
  gatewayName: acmehrgateway
  knowledgeBaseName: acmehr-knowledge-base
  kbPrefix: acmehr/
  ssmParameterPrefix: /app/acmehr
  lambdaFunctionName: acmehr-tools-handler
agent: {audience, purpose, scope[], outOfScope[], roles[], handoffConditions[], prohibitedBehaviors[]}
facts:
  - id: pto-days
    statement: Full-time employees accrue 15 PTO days per year.
    criticality: blocking
    provenance: customer_confirmed
    confirmedBy: Jane Doe (HR Director)
    confirmedAt: 2026-09-01T09:00:00Z
    confirmationRef: intake call notes 2026-09-01
knowledge:
  documents: [{id, title, file, provenance, noise}]
  noisePlan: {enabled: true, rationale: ...}
tools:
  - name: get_pto_balance
    description: ...
    inputSchema: {...}   # only type/properties/required/items/description (AgentCore Gateway); put enums, ranges, defaults in description
    fixtures: {cases: [...], default: {...}, errors: [...]}   # generic fixture-driven Lambda
    provenance: sa_synthetic
skills: [{name, file}]
prompts: {baselineFile: agent/baseline-prompt.md, optimizationCandidateFile: agent/optimization-candidate.md}
evaluation:
  retrievalToolName: retrieve_hr_policy
  judgeModel: us.amazon.nova-2-lite-v1:0
  goldenSet:
    - id: pto-accrual-normal
      label: PTO accrual (normal)
      query: How many PTO days do I get per year?
      category: normal            # normal | boundary | prohibited
      set: practice               # practice | holdout
      actorId: employee-001
      roleId: employee
      expected: {mustMention: ["15"], requiredTools: [retrieve_hr_policy]}
      basis: [pto-days]
      provenance: customer_confirmed
labs:
  observations: [...]
  teaching: {firstConversation, baselineDefects, phenomena, stabilityCaseId}   # required, see §3b
  guide: {id: guide-narrative, provenance: ai_draft, memoryLesson, retrievalContrast, ...}   # optional, see §3c
```

Ids: `^[a-z][a-z0-9-]{1,60}$`, unique across facts, documents, tools and golden cases. Fixture
placeholders: `{{arg:x}}`, `{{year}}`, `{{uuid4}}`, `{{now}}`.

## 3b. Teaching skeleton (`labs.teaching`)

A pack replaces the HR workshop only if it reproduces the HR lessons in the customer's domain.
`labs.teaching` declares them and Validate checks them (`teaching.*` findings); a pack without it
does not build (`teaching.missing`). The three reference packs are worked examples: hr-default
(buried sick-leave gap, as upstream), it-helpdesk and maintenance (absent gaps with bait).

| Teaching moment | Script (Guide step) | What the pack supplies |
|---|---|---|
| Noisy knowledge base; "SP1 high, SP2 low = dirty sources" | 01, 09 (3, 12) | `noise: true` documents with an off-topic FAQ tail, `noisePlan.enabled`, a `noise_grounding` phenomenon |
| Tools through the Gateway | 02 (4) | one `retrieval` tool (named `retrieve_*` by convention) plus mock tools whose names never contain `retrieve`, `knowledge`, `kb_` or the retrieval tool's name |
| Weak baseline deployed | 04 (6) | a baseline that names the retrieval tool / tool target but has no grounding rules; `baselineDefects` |
| "The agent doesn't know you yet" (Memory) | 06 (8, 10) | `firstConversation` {query, actorId, unknownContext, label, mustNotMention} — not a golden case, never a holdout query; `mustNotMention` holds 1–3 values only a remembered user would get (their own record id from a fixture), so rehearsal can check the Memory lesson (else `MEMORY_UNCHECKED`) |
| Golden evaluation (THELMA + Mind the Goal) | 09 (12) | 3–4 retrieval **probes**: practice cases of `prompt_fixable` / `retrieval_gap` phenomena |
| Prompt optimization closes the loop | 10 (13) | every defect's `candidateFix` copied verbatim from the candidate (in the KB language) and absent from the baseline |
| Core contrast: fix the prompt vs fix retrieval | 09 vs 10 | ≥ 2 `prompt_fixable` probes whose answers are literally in noise documents; ≥ 1 `retrieval_gap` (`absent`: the fact is in no document; `buried`: the answer is hidden behind bait) |
| Cost / model comparison / judge stability | 11, 12, 13 | nothing extra: they re-use the probes; `stabilityCaseId` (a prompt_fixable probe) is asked last |
| Tool and permission lessons | 09/10 (L1 + Mind the Goal) | ≥ 1 practice `tool_use` case (a mock tool, a fixture `when` value in the query) and ≥ 1 practice `refusal` case (prohibited, `shouldRefuse`, plus `forbiddenTools` or `mustNotMention`); `escalation` optional |

Probe recipe (every probe): `set: practice`, category `normal` or `boundary`, `requiredTools`
exactly `[<retrievalToolName>]`, no `shouldRefuse`, a purely informational question (no mock
action, no fixture value, no near-copy of a holdout question). `prompt_fixable` and `buried`
probes carry 1–4 literal `mustMention` answer terms that the KB contains; an `absent` probe sets
`shouldEscalate: true`, names the hand-off target and lists `absentTerms` that no document contains.
Bait (`baitTerms`, copied from the question) must appear ≥ 3 times in `noise: true` documents and
never in the same paragraph as an answer term. Size targets: 12–16 golden cases, 5–7 practice,
holdout ≥ 1/3, 5–9 documents. 09/10 ask non-probes first, then the probes, with the stability case
last; every practice case runs as its own fresh actor, so authored `actorId`s may be shared.
Noise documents, bait and the absent gap are synthetic teaching settings (`origin.kind:
teaching_design`), never customer facts.

Main `teaching.*` gate rows (errors block the build; warnings are advisory):

| Code | Severity | Meaning |
|---|---|---|
| `teaching.missing` | error | no `labs.teaching` |
| `teaching.too_few_probes` / `too_few_fixable` / `no_gap` | error | fewer than 3 probes / 2 prompt_fixable / 1 retrieval_gap |
| `teaching.probe_tools` / `probe_category` / `probe_refuse` / `probe_needs_mustmention` | error | a probe is not a retrieval-only, answerable normal/boundary question with answer terms |
| `teaching.missing_kind` / `tool_use_case` / `refusal_case` / `escalation_case` | error | noise_grounding, tool_use or refusal missing, or their cases are malformed |
| `teaching.defect_fix_missing` / `defect_already_fixed` / `defect_marker_*` / `defect_unexercised` | error | a baseline defect is not a real baseline-vs-candidate difference |
| `teaching.baseline_no_retrieval_hint` | error | the baseline never names the retrieval tool or tool target (no THELMA traces) |
| `teaching.fixable_answer_not_in_kb` / `buried_answer_not_in_kb` / `absent_term_in_kb` / `absent_needs_escalation` | error | the KB does not match the declared outcome |
| `teaching.bait_not_in_query` / `bait_missing` | error | bait is not the question's wording, or no noise document carries it |
| `tools.retrieval_marker_collision` | error | a mock tool (or the tool target) contains `retrieve`, `knowledge`, `kb_` or the retrieval tool's name, so THELMA would score it as retrieval |
| `tools.retrieval_count` / `tools.retrieval_query_schema` | error | not exactly one `retrieval` tool, or its inputSchema does not require a string `query` (the Lambda forwards only `query`) |
| `namespace.agent_name_too_long` / `kb_name_too_long` / `evaluator_name_too_long` | error | `agentName` over 19, `knowledgeBaseName` over 50, or `<agentName>_thelma_rag_quality` over 48 characters (AWS name limits; the app derives generated names within them) |
| `namespace.agent_name_reserved` | error | `agentName` is `current`, `previous`, `releases`, `run` or `skills`, entries of the Workshop instance's `~/workshop` that 04-deploy and 99-cleanup.sh would remove (the app suffixes such a project's agentName with `agent`) |
| `teaching.gap_only_buried`, `bait_weak`, `bait_near_answer`, `probe_tool_overlap`, `probe_fixture_value`, `probe_near_holdout`, `gap_near_holdout`, `prose_names_holdout`, `retrieval_case_not_probe`, `many_probes`, `too_many_practice`, `no_skill`, … | warning | the contrast may not reproduce reliably, or student prose names the holdout; rehearsal is the proof |

### L1 expectations (deterministic answer checks)

- `mustMention`: 1–4 short literal tokens (numbers, codes, names), ≤ 40 characters; use
  `mustMentionAnyOf: [[...], ...]` for paraphrasable alternatives.
- `mustNotMention`: only unambiguous violations (a leaked value, "has been disabled") — never a word
  a correct refusal repeats ("disabled" for a disable request) or a unit an honest "not in the KB"
  answer may name.
- `evaluation.l1.escalationMarkers`: the scenario's hand-off phrases ("security team", "raise a work
  order"), declared whenever a case sets `shouldEscalate`; `escalationTools` only for a mock hand-off
  tool; `refusalMarkers` only to replace the built-in defaults.
- `evaluation.judge.userLabel`: how Mind the Goal labels the user's turns (hr-default: `Employee`).
  Mind the Goal judges with `agent.handoffConditions` and `agent.prohibitedBehaviors`, so a correct
  refusal or hand-off reads as success.
- Validate warns (`l1.*`, advisory) about a practice case L1 cannot check (`no_assertions`), a
  `shouldEscalate` case with no escalation marker/tool and no `mustMention` hand-off target
  (`escalation_deferred`), a lexical term over 40 characters (`long_phrase`) and a tool name
  containing `___` (`tool_name_separator`).

## 3c. Generated Workshop Guide

Every build writes two guides in `scenario.language` (set it to `zh-CN` when the content is Chinese):

- `pack/labs/student-guide.md` = the release root `README.md` (participants): the upstream Guide's
  steps and its 28 command lines, with this scenario's questions, names, contrast and a "what you
  should see" table derived from `labs.teaching`. It never shows holdout cases, `expected` answers,
  confirmers or observations; customer-confirmed facts and synthetic teaching settings sit in
  separate tables (`[Customer-confirmed]` / `[Synthetic teaching setting]`).
- `instructor/instructor-guide.md` (never in a release): answer key including the holdout, truth
  sheet, contrasts, readiness checklist, facilitation notes, rehearsal evidence (verdict, reason,
  readiness blockers, noise band, missing evidence), and `hr-default` as the class fallback — only in an
  environment prepared and rehearsed for it (the add-ons are bound to one pack namespace).

`labs.guide` is optional teaching prose (defaults come from `labs.teaching`): student fields
`tagline`, `scenarioIntro`, `memoryLesson`, `retrievalContrast`, `stepNotes`, student `experiments`,
`attribution`; instructor fields `facilitatorNotes`, `designRationale`, instructor `experiments`.
Prose only: no code fences or command lines, no headings, no HTML, links only in `attribution` and
instructor fields, never a holdout case or the word holdout in student fields. Supplement files
(`labs.studentGuideFile` / `instructorGuideFile`) must be UTF-8 (`guide.supplement_encoding`). It is one review item,
`guide-narrative`: `ai_draft` blocks the build, review it as `sa_synthetic` (never customer_confirmed).
`labs.studentGuideFile` / `instructorGuideFile` are embedded supplements and need `labs.guide`.
Preview both guides before a build (`GET .../guide/preview?audience=`, or
`python -m workshop_customizer guide <scenario> --audience student --draft`); after the build read
`GET .../guide?audience=` and download the instructor bundle (`GET .../export?bundle=instructor`,
contains the holdout — never hand it to participants).

## 4. Golden set policy (enforced by the engine)

- ≥ 12 cases; ≥ 3 per category (`normal`, `boundary`, `prohibited`); ≥ 1/3 `set: holdout`.
- Each case cites `basis:` fact ids; prohibited cases expect a refusal (`shouldRefuse: true`)
  grounded in a prohibited behavior; escalation cases set `shouldEscalate: true`.
- Holdout is **instructor-only**: never in student documents, prompts, skills or the release zip.
- Judge noise band: keep `evaluation.noiseBand` (from the upstream judge-stability script) in mind —
  a change that does not clear the band is not an improvement.

## 5. Gate table (one authoritative list, all fail closed)

| Gate | Where | Blocks |
|---|---|---|
| Schema (JSON Schema 2020-12) | `Validate` | build |
| Cross-references (files exist, ids unique, basis/roles resolve) | `Validate` | build |
| Provenance gate (`customer_confirmed` on blocking items of customer packs; origin-labeled `sa_synthetic` + customer anchors in workshop packs; no `ai_draft`/`pending`; no simulated confirmation; §2) | `Validate` | build |
| Golden-set policy (counts, categories, holdout ratio) | `Validate` | build |
| Teaching skeleton (`labs.teaching`: probes, gap, defects, first conversation, tool/refusal cases; §3b) | `Validate` | build |
| Guide narrative (`labs.guide` prose rules, no holdout in student prose; `guide.*`; §3c) | `Validate` | build |
| Guide output (28 upstream commands, no holdout in the student guide, domain residue, instructor marker never in the release, `README.md` = student guide) | `Build` | release |
| Workspace (`provenance.material_unknown`: an origin cites a material that is not uploaded, the SA's to re-label on reviewed items; orphan files are warnings the next Kiro apply prunes) | `Validate` | build |
| Draft-mode dry build (compile + render into a throwaway dir: holdout, residue, secrets, render errors) | `Validate` | build |
| Holdout isolation (no holdout text in student files) | `Build` | release |
| Namespace/upstream residue (no `hr_tools_handler`, `hr/`, old names) | `Build` | release |
| Release integrity (per-file SHA-256, contentHash, RELEASE.json) | `Build`, `Export`, `Sync` | sync |
| Sync preflight (identity/account, credential TTL, stacks, target contract vs template commit, instance online, **no scenario AWS resources yet**) | `Sync → Preflight` | apply |
| Explicit confirmation (single-use token bound to the plan digest, 15 min) | `Sync → Apply` | apply |
| Host smoke + watchdog (integrity, `bash -n`, py_compile, readability; auto-rollback) | EC2 applier | commit |

## 6. Runbook order (before Workshop day)

1. Ops (once per workshop environment **and pack namespace**): deploy `sync/cfn/customizer-addons.json`
   with `tools/deploy_addons.py --scenario <this project's scenario.yaml>` (workshop stack name,
   instance id and pinned template commit come from the stack); attach the `${StackName}-sync` managed
   policy to the SA's temporary role. No long-lived keys anywhere. The add-ons grant the Workshop EC2
   its permissions for that one namespace: preflight `stack.addons.namespace` fails when the release's
   namespace differs — redeploy the add-ons for the new pack (after `99-cleanup.sh --scenario-only` of
   the earlier release; preflight `guard.previous.*` checks its resources are gone).
2. SA: `aws sso login` (or assume the temporary role) in a terminal; name that profile in the app's
   *Workshop 环境* (03 交付). The app stores only names — profile, region, expected account id, stacks.
3. SA: Validate → Build → (optional) Export zip for review → Preflight → read the plan → Apply (03 交付
   → *一键覆盖* runs them as one job; 高级 → 分步同步 runs them one by one).
4. SA: facilitator smoke on the EC2 (upstream `00-verify-setup.sh` path) → *确认保留* (Confirm release,
   not a Git commit). Anything wrong → *回滚* (atomic `current` symlink switch back to `previous`).
5. Sync is only allowed **before** the scenario's AWS resources (KB, Gateway, Harness, evaluators)
   exist. To re-sync a changed pack into the same SA-prepared environment, first run
   `cd ~/workshop/current && ./99-cleanup.sh --scenario-only` on the Workshop instance (or
   `WORKSHOP_CLEANUP_SCENARIO_ONLY=1 ./99-cleanup.sh`; never automatic): it removes only this
   scenario's agent, Gateway, knowledge base, tools Lambda, SSM parameters and eval-run records and
   keeps `workshop-infra` and the Customizer add-ons. Plain `./99-cleanup.sh` also deletes the add-ons
   stack and `workshop-infra` — that is the after-class teardown, never a step of the rehearsal loop.
   `current` cleans only its own namespace. Every release's `99-cleanup.sh` has its own namespace's
   names rendered in (agent, AgentCore stack, Gateway, knowledge base, tools Lambda, SSM prefix,
   `~/workshop/<agentName>`) and runs its helpers from its own directory, so run from another release's
   directory it removes that release's namespace and leaves `current` alone. When the guard names
   another namespace's resources (`guard.previous.*`, or this pack's own while `current` is another
   pack's):
   1. Bind the add-ons to that namespace: they grant the Workshop EC2 the deletes (agent, Gateway, SSM
      parameters, tools Lambda) only for the namespace they are bound to —
      `tools/deploy_addons.py --scenario <that pack's scenario.yaml>`. `--scenario` is read only for its
      `namespace`, so when that pack's scenario file is gone (the app keeps only the current build's
      snapshot), the release's own `RELEASE.json` works: it parses as YAML and carries the same six names
      (from `~/workshop/releases/<release>/` in step 2, or from `release.zip` in the release bucket).
   2. Find its release; this lists every release on the host, newest first, with its agentName and SSM
      prefix:
      ```bash
      cd ~/workshop/releases && for r in $(ls -t); do echo "$r $(python3 -c 'import json,sys; n=json.load(open(sys.argv[1]))["namespace"]; print(n["agentName"], n["ssmParameterPrefix"])' "$r/RELEASE.json")"; done
      ```
   3. `cd ~/workshop/releases/<release> && ./99-cleanup.sh --scenario-only`. Under another namespace's
      binding it prints ❌ lines naming AccessDenied and exits 1: bind (1), then re-run it.
   4. Bind the add-ons to the new pack again (`--scenario <the new pack's scenario.yaml>`; nothing to do
      when it was the new pack's namespace), then run preflight.

   The host never prunes the newest release of a namespace, so one is there for every namespace it
   received; a release of that namespace unzipped anywhere on the host (an exported bundle) cleans the
   same.
6. Rehearsal (SA-prepared final environment). **Predict it first, locally** (minutes, no AWS resources):
   `tools/pre_rehearsal.py --project-dir <project> --profile <p>` (the headless loop: `--pre-rehearse`) runs
   every practice question on the Workshop model and judges the tool_use / refusal / escalation phenomena
   with L1 and the retrieval gap with the Workshop's own THELMA code (it scores the agent's first search, and
   the question itself as one). A gap it predicts `likely_not_reproduced` failed live 4 of 4 times, and one it
   predicts `likely_reproduced` reproduced 4 of 4 (backtest of 2026-10-01): repair such a gap before the
   42-minute run. Its
   prompt_fixable readings are shown only (9 of 24 matched live). **Rehearse it directly** next (about 8 min,
   no Workshop instance): `tools/direct_run.py --project-dir <project> --profile <p> --account <id>` creates the
   pack's own knowledge base, tools, memory and Harness on AgentCore (resource names end in `-direct`, the
   Harness is `direct_<agent>`: no Workshop selector matches them), asks every practice question with both
   prompts, judges them with the Workshop's evaluators and writes `build/direct/<version>/direct-rehearsal.json`;
   a repair reads it as rehearsal findings (`kiro_generate --loop --build --direct` does both until ready).
   `--repeat 3` runs three rounds after one provisioning and reports how often each phenomenon reproduced
   (`direct.robust`: every round ready; with `kiro_generate --direct-repeat 3` the loop repairs a phenomenon that
   reproduces in some rounds only). `--panel` also scores the sessions with AgentCore's built-in evaluators and
   says which of them see each phenomenon, scored as an online evaluation scores them (no golden expectations);
   `tools/online_eval.py` turns that into an AgentCore online evaluation of the class runtime or the direct Harness
   (a plan unless `--apply`; it bills per sampled session until `--delete`), and `--results` lists the failing
   live sessions with their questions, as golden-case candidates for Kiro. `tools/verify_agent.py --harness <name>
   --contracts <file>|--from-pack <project>` verifies any Harness in the account the same way (it is only invoked).
   `tools/direct_fleet.py
   --data <App data dir>` rehearses every built pack at once (a drift check); `--cleanup` removes a pack's
   direct resources. The App's 04 page runs the same (快速彩排 · 直连 AgentCore, with a chat). A direct verdict
   is never the class verdict. Then run all 15 Guided steps (04 彩排与上课), then *记录彩排结论*
   (`POST run/rehearsal`; CLI `python -m workshop_customizer rehearsal <project-dir>`). It judges every
   `labs.teaching` phenomenon on the latest complete run of this release (earlier runs are listed with
   their consistency) and writes `run/rehearsal.json`. `readyForClass` = verdict `ready` ∧ complete
   report ∧ both guides built — nothing else says a pack is ready. Work through `remediation[]`
   (blocking entries first); whether it needs a new release decides the loop:
   - **Same release** — the entry names no project file (`asset.kind` `ops` or `run`, or re-running
     13 for the noise band): fix the environment (for example redeploy the add-ons RunStep document),
     reset from the step the entry names (usually `baseline`), run to the end and rehearse again.
     Runs of one release accumulate; two consistent complete runs are the safer bar.
   - **New release** — the entry names a project file (KB doc, baseline/candidate prompt, golden case,
     `labs.teaching`, a tool fixture, `evaluation.*`): **Repair** with Kiro (§7.4: a repair of the
     current release reads the blocking remediation of `run/rehearsal.json` as findings R1..Rn;
     headless: `tools/kiro_generate.py --loop --project-dir <project> --from-rehearsal`), then Validate
     and Build — never a hand edit. That is a new release
     version. A partial reset cannot cross releases, and sync preflight fails while the rehearsal
     run's KB, Gateway or runtime exist (step 5). So first remove the rehearsal resources by hand with
     `./99-cleanup.sh --scenario-only` on the Workshop instance (step 5; never automatic — plain
     `99-cleanup.sh` would delete `workshop-infra` and the add-ons this environment needs), then
     Preflight → Apply → Commit, reset the Guided Run fully (no `fromStep`) and run all 15 steps again.
     Earlier releases' runs do not count toward the new one.
     **Check the repair first** (same namespace only): `tools/fast_rehearse.py --project-dir <project>
     --profile <p> --account <id>` runs the new build on the resources still there — its own 01/02/03 update
     the KB, tools and skills in place, update-harness sets the new baseline prompt, and its 06/09/10/13 run
     and are judged by the rehearsal rules — in about 18 min instead of about 59 (live 2026-10-01: same verdict
     and blocking cause as the regular round). Its `readyForClass` is always false; when it says ready, do the
     cleanup and full run above once for the class verdict. When it does not, repair again first.

   Holdout cases are never executed by rehearsal (instructor-only; the instructor guide explains
   manual use).

Never describe the Workshop-day validation as a production launch: it validates the *workshop*
pack, not the customer's production agent.

## 7. Materials → Generate → Validate → Repair loop

1. **Materials** (optional): upload customer files (md, txt, csv, json, yaml, docx, pptx; pdf only
   when the backend has `pypdf`) — at most 20 files, 5 MiB each, 30 MiB in total. Each has a
   `generationUse`: `source` (Kiro may derive facts, documents and fixtures and must cite it),
   `background` (tone and scope only; never amounts, deadlines, permissions or thresholds) or
   `exclude` (stored, never sent). Uploads with credential or national-id shapes are refused.
   Record the materials approval (who approved sending them to the Kiro model, and the data class:
   `synthetic` or `internal`) before generating with any material, SA-written ones included;
   `confidential` material is never sent. Materials are never pack sources: they stay out of the pack
   digest, build, release and export, and a `fromMaterial` file copies only a material sent in that task.
2. **Generate** (`draft`, 1200 s): Kiro returns a complete pack after the task's SELF-CHECK: golden
   counts per category and set with the holdout arithmetic (15 cases need 5 holdout), every `basis` id a
   fact id, every baseline defect listed by a probe phenomenon, absent terms and the gap question in no
   document, verbatim `candidateFix` / `baselineMarker`, control designs, `firstConversation.mustNotMention`.
   The app folds one mechanical slip itself: a golden `basis` entry that names a knowledge document (id,
   file or file name) becomes the one fact whose `source` names that document, or is dropped when no fact
   names it and the case already cites a fact. It folds only when every fact carries a `source` (the
   contract asks for one): without that, which facts a document states is unknown. Any other shape stays
   an `xref` finding for Repair. Each fold is listed in
   the generation warnings, and a reviewed case whose basis folds loses its review. **Apply** needs an explicit
   acknowledgement (and a second one when customer confirmations would reset, removed items
   included). Apply deletes orphan files (unreferenced files under `knowledge-base/docs/`, `agent/`,
   `skills/`, `guides/`, `tools/`, and `generate_content.py`) behind a snapshot; the current UI's
   bare Apply keeps them, and `POST /projects/{pid}/files/prune` (dry run first) moves them to
   `generation/pruned/`. **Revert** restores the exact pre-apply bytes while the project is unchanged
   since that apply (the newest five snapshots are kept).
3. **Validate** (draft mode, dry build): every finding carries `scopes`; `build/validate.json` lists
   them as numbered `repair.repairable` (Kiro can fix them), `saOnly` (reviews, confirmations,
   anchors) and `engine` findings, with `suggestedScopes`.
4. **Repair** (`repair`, 900 s): works only from a fresh `validate.json`; Kiro fixes the numbered
   findings and SA instructions inside the allowed scopes, returns the full scenario plus only the
   changed files, and records each change. A repair keeps the golden set: Kiro never deletes, renames or
   re-categorises a case unless a finding (or your instruction) names it, and keeps the counts
   (holdout at least a third, at most 7 practice cases) by moving unused practice cases to holdout —
   also before any count finding exists, when a named fix such as a holdout case moved to practice
   would break them.
   Everything outside the scopes is restored from disk, and the project's namespace (its deployed AWS
   names) is kept. When `run/rehearsal.json` judged the
   current build, its blocking remediation that names a project asset joins the findings (R1..Rn) and
   its scopes join the allowed scopes (`includeRehearsal: false` leaves it out); environment hints
   (ops, run, judge) stay the SA's steps in the Workshop environment.
   **Regenerate** rebuilds the whole pack or one scope (for example `guides`) from the brief and the
   current pack, also with no finding and no instruction (a repair changes only what its findings and
   instructions cite); like a repair it keeps the golden set — case ids, categories, sets and counts —
   unless a finding or your instruction names a case.
5. Review in the app (Record confirmation / Batch review), then Validate and Build. Never hand-edit
   files to get past a gate: `editLog` counts manual saves, and the headless driver
   (`tools/kiro_generate.py --loop`) fails a run that needed them.
