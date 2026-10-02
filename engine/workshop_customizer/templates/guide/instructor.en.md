{{marker}}
{{instructor_banner}}
{{draft_banner}}
# Instructor guide: {{title}}

{{meta_table}}

The participants' guide is the release's `README.md` (a copy is `pack/labs/student-guide.md`). This guide
is never part of a release: it travels only in the instructor bundle of the App.

## Contents

{{toc}}

---

## 0. Before class: readiness

### Provenance

{{provenance_matrix}}

{{waived_line}}

{{provenance_banner}}

### Customer-confirmed anchors

{{anchors_table}}

{{anchors_note}}

{{classification_line}}

### Teaching declarations

{{declarations_table}}

### Validator warnings

{{warnings_list}}

### Rehearsal sign-off checklist

{{checklist}}

---

## 1. Run sheet

One row per guide step. The Guided Run of the App runs the 15 steps from `00-setup.sh` to
`13-judge-stability.sh` through the fixed SSM runner, bound to the active release; participants run the
same scripts from `~/workshop/current/static/scripts`.

{{runsheet_table}}

---

## 2. Truth sheet

### 2.1 Customer-confirmed facts

May be presented as the organization's actual rules.

{{truth_customer_facts}}

### 2.2 Synthetic teaching settings

Present them as synthetic teaching settings, never as the organization's policy.

{{truth_synthetic_facts}}

### 2.3 Knowledge documents

{{truth_documents}}

{{noise_plan_text}}

### 2.4 Tools

{{truth_tools}}

### 2.5 Prompts

{{defects_table}}

{{prompt_diff}}

{{design_rationale}}

---

## 3. Teaching contrasts

### 3.1 Memory (first conversation)

{{memory_contrast}}

### 3.2 Prompt vs retrieval

{{retrieval_contrast}}

If it does not reproduce in class: check the number of retrieval traces 09 found against
{{probe_count}}; read `quality.comparisonWarnings` (different sample counts make the mean difference
descriptive only); compare the change with the noise band; re-run 09/10 once. Never edit the pack
mid-class — every change needs a new release, a new sync and a new rehearsal.

### 3.3 Tool use, refusal and hand-off (L1 vs L2)

{{l1_contrast}}

L1 decides these questions deterministically; when a check can only be deferred (a refusal or hand-off
without a marker, a negated forbidden phrase) the Mind the Goal verdict resolves it. Mind the Goal judges
with the scenario policy, so a refusal or hand-off the policy requires is a success, not RCOF E2.

### 3.4 Noise, model comparison and judge noise

{{noise_contrast}}

`12-compare-models.sh` re-asks every practice question on the comparison model and restores the
baseline model; read quality, latency and cost side by side. `13-judge-stability.sh` re-scores the last
retrieval trace ({{stability_case}}) {{stability_runs}} times; the report derives the noise band as
max(2σ, spread, 0.02) unless `evaluation.noiseBand` is calibrated. A band above {{band_max}} makes
THELMA-judged contrasts insufficient (`JUDGE_TOO_NOISY`).

---

## 4. Answer key

### 4.1 Practice cases

Listed in the order 09 and 10 ask them.

{{answer_practice}}

### 4.2 Holdout cases

{{answer_holdout}}

### 4.3 Using the holdout

{{holdout_use}}

---

## 5. Reading results

The console output is described in step 12 of the participants' guide; the tables are repeated here.

{{thelma_legend}}

{{diagnosis_table}}

{{diagnosis_levels}}

{{mtg_reading}}

{{rcof_table}}

{{l1_statuses}}

{{l1_compare}}

{{retry_messages}}

The App report (`run/report.json`) reads the build snapshot of the release the run executed:

{{report_fields}}

---

## 6. When results differ

The report's `teachingContrast` names a code per case. What to say and do:

{{facilitation_table}}

---

## 7. Troubleshooting

{{troubleshooting_table}}

---

## 8. Discussion experiments

{{instructor_experiments}}

---

## 9. Rehearsal evidence

{{rehearsal_block}}

---

## 10. Cleanup, fallback and rollback

`99-cleanup.sh` is never run automatically and is not part of the Guided Run; run it (or let a
participant run it) only after class. It deletes `{{agent_name}}`, `{{gateway_name}}`, `{{lambda_name}}`,
`{{kb_name}}`, the SSM parameters under `{{ssm_prefix}}`, the eval-run records, the
`workshop-customizer-addons` stack and then `workshop-infra`; exit status 75 means AgentCore ENIs are
still attached — re-run it later. Before class, to re-sync a changed pack into this environment (a new
release after rehearsal), run `./99-cleanup.sh --scenario-only` instead (or
`WORKSHOP_CLEANUP_SCENARIO_ONLY=1`): it removes only this scenario's resources and the eval-run records and
keeps `workshop-infra` and the add-ons; then Preflight → Apply → Commit and reset the Guided Run fully.
To fall back, sync the previous release from the App (rollback switches
`~/workshop/current` back); a rollback swaps release files only and never undoes AWS resources the
scripts created.

{{fallback_line}}

---

## Appendix A. Names and filters

{{names_table}}

Retrieval-trace filter of 09/10/11/13: `{{retrieval_span}}`. Run records:
`~/workshop/eval-runs/{{agent_name}}/<baseline|optimized|comparison>-<epoch>/` (`sessions.tsv`,
`q<i>.out`, `scores.tsv`, `l1.json`).

## Appendix B. Lab observations

{{observations}}

## Appendix C. Facilitator notes (scenario author)

{{supplement}}
