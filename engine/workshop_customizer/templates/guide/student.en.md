{{marker}}
{{draft_banner}}
# Eval-First: {{title}}

**{{tagline}}**

---

> [!NOTE]
> This is the hands-on guide of the **"Eval-First: Building Enterprise Agents with AgentCore"** workshop,
> customized for this scenario. The knowledge-base documents, the tools Lambda fixtures, the Skills, the
> prompts and the practice golden questions come from `pack/`; the infrastructure, the evaluators and the
> script order are the pinned template's. Running the scripts in order builds the whole agent in the
> Workshop environment your facilitator prepared for this scenario (see [Prerequisites]({{anchor_prerequisites}})).

> [!IMPORTANT]
> **Follow this guide top to bottom.** Each step lists what it does, what it needs, and what it
> produces. Run the scripts **in the numbered order** — each one prints `Next: ...` pointing at the
> following step.

{{generated_by}}

---

## Table of contents

{{toc}}

---

## 1. The scenario

{{scenario_intro}}

{{scenario_table}}

### Roles

{{roles_table}}

### What is real and what is synthetic

{{provenance_banner}}

**Customer-confirmed facts** — the only statements that describe the organization's actual rules:

{{customer_facts}}

**Synthetic teaching settings** — written for this class; never quote them as the organization's policy:

{{synthetic_facts}}

{{asset_summary}}

---

## 2. What this builds — and why

This workshop is **eval-first**: the whole point is to stand up a realistic enterprise agent and then
**measure its quality with code-based evaluators**, rather than eyeballing a few answers. The scripts
build **{{title}}** (Knowledge Base + Gateway tools + Memory + Skills on Amazon Bedrock AgentCore), run
it to produce traces, and then score those traces with two custom evaluators and a set of
deterministic code checks.

The two evaluators are the heart of the workshop. They live in `evaluators/` and are independent
re-implementations of **published research methods** (not AWS products), wired to run on Amazon
Bedrock AgentCore:

### `thelma_eval/` — single-turn RAG quality (THELMA)

Runs at **`TRACE`** level (the *glass-box* granularity — it inspects one execution trace).
Decomposes one Q&A into `(question, retrieved sources, answer)`. The THELMA paper defines **6
metrics**; this implementation reports **Source Precision as two separate scores** (chunk-level vs.
fact-level), so you'll see **7 numbers** per trace (all 0–1):

| Metric | Name | Question it answers |
|:------:|------|---------------------|
| **SP1** | Source Precision (chunk) | Are the retrieved **chunks** relevant as a whole? |
| **SP2** | Source Precision (fact)  | Of the **facts inside** those chunks, how many are actually relevant? |
| **SQC** | Source Query Coverage   | Do the sources cover the question? |
| **RP**  | Response Precision      | Is the answer on-topic? |
| **RQC** | Response Query Coverage | Is the question fully answered? |
| **SD**  | Self-Distinctness       | No internal repetition? |
| **GR** | **Groundedness** | **Is every sentence backed by a source? (no hallucination, pass ≥ {{gr_pass}})** |

Its real value is **diagnosis** — the *interplay* of these scores points at which RAG component to fix
(retriever vs. prompt vs. source docs). The SP1/SP2 split is the key example: a **high SP1 with a low
SP2** means the chunk looks on-topic but most of the *facts* it carries are noise — exactly the symptom
of dirty data mixed into the source documents. The patterns the evaluator prints:

{{diagnosis_table}}

### `mtg_eval/` — multi-turn goal success (Mind the Goal)

Runs at **`SESSION`** level (the *black-box* granularity — end-to-end goal outcome) in three steps:
**segment goals** (merge turns about the same thing), **judge success/failure** (a goal fails if any
turn fails), then compute **GSR** = *Goal Success Rate* (successful goals ÷ total goals, pass ≥ {{mtg_pass}}%) and
attribute each failure via **RCOF** = *Root Cause of Failure* (7-category defect taxonomy). Answers
*"did the agent actually accomplish what the user came for?"* In this release the judge prompt carries
this scenario's policy (scope, hand-off conditions and prohibited behaviors from §1), so a refusal or a
hand-off the policy requires counts as a success, not as *Refusal to Answer*.

### `l1_eval.py` — scenario assertions (L1 code checks)

`09-run-eval.sh` also runs `l1_eval.py`: deterministic checks of **every** practice question against its
golden expectations — required and forbidden tool calls, phrases the answer must or must not contain,
and whether it refuses or hands off when it should. They cost nothing, never fluctuate, and cover the
questions THELMA does not score (tool lookups, refusals, hand-offs). A check L1 cannot decide is
deferred to the Mind the Goal verdict.

> [!NOTE]
> The **TRACE → glass-box** and **SESSION → black-box** mapping is deliberate: AgentCore's
> session / trace / span levels line up with the three evaluation granularities (black-box / glass-box
> / white-box) from the companion white paper. The two evaluators are **custom L2 evaluators**
> (calibrated LLM-as-a-judge) in that framework, and `l1_eval.py` is its **L1** layer — see
> [below]({{anchor_methodology}}).

The judge model is `WORKSHOP_JUDGE_MODEL` from `00-config.sh` (by default the agent model,
`us.amazon.nova-2-lite-v1:0`); this scenario declares `{{judge_model}}`, and `08-create-evaluators.sh`
prints the model it sets. Each evaluator bundles its algorithm, an **adapter layer** (ADOT span →
evaluator input), and a Lambda handler.

> [!TIP]
> See **[`evaluators/README.md`](evaluators/README.md)** for the full metric definitions, the THELMA
> diagnosis table, paper citations, and licensing.

{{noise_paragraph}}

---

## 3. Architecture at a glance

The agent runs as an AgentCore **Harness** in VPC mode. Every invoke pulls Memory + Skills into
context, calls the scenario's tools through the **Gateway** (MCP), and emits OTel trace spans that
flow to CloudWatch — where the evaluators read them.

{{architecture_diagram}}

> Solid lines are the live call path; dashed lines are trace/judge flow.

{{names_table}}

---

## 4. The eval-first loop (ADLC)

The workshop closes the **Agent Development Life Cycle**: build, run, trace, evaluate, _diagnose_, then
optimize — and prove the fix with a re-evaluation.

1. **Build** (once): steps 1–7 create the knowledge base, the tools, the Skills and the agent.
2. **Run**: steps 8–10 and `09-run-eval.sh` ask the questions.
3. **Trace**: every answer leaves spans in CloudWatch `aws/spans`.
4. **Evaluate**: THELMA scores the retrieval traces, Mind the Goal every session, L1 every question.
5. **Diagnose**: the score pattern says whether to tighten the prompt (stay on the ring) or fix
   retrieval (a branch out, when SP2 is near zero).
6. **Optimize**: `10-optimize-prompt.sh` installs the optimized prompt — and re-evaluates to prove it.

> [!TIP]
> {{adlc_tip}} That contrast is exactly how THELMA distinguishes *"fix the Prompt"* from *"fix retrieval."*

---

## 5. How this maps to the eval-first methodology

This workshop is the **hands-on companion** to a four-part white paper on production-grade enterprise
agents. Where the white paper gives the *why* and the framework, this release lets you run it end to
end. The mapping:

| White-paper concept | What you run here |
|---------------------|-------------------|
| **ADLC** — the build → run → trace → evaluate → diagnose → optimize flywheel | The whole script sequence; `10-optimize-prompt.sh` closes the loop |
| **Three evaluation granularities** — black-box / glass-box / white-box, aligned to AgentCore **session / trace / span** | **Mind the Goal = SESSION (black-box)**, **THELMA = TRACE (glass-box)** |
| **Three-layer evidence weighting** — L1 code / L2 calibrated LLM-judge / L3 refuse-by-default | **L1** = `l1_eval.py`, run by `09-run-eval.sh` on every practice question; THELMA & Mind the Goal are **custom L2 evaluators**; `13-judge-stability.sh` checks L2 reliability, with human TPR/TNR calibration as the L3 complement |
| **Decision-first KPIs** — Decision Quality / Time-to-Action / Cognitive Offload | quality (GR) + speed & cost (`11-cost-latency.sh`) give you the first two dimensions as hard numbers |
| **AgentCore Evaluations** — built-in + custom evaluators | Both evaluators here are **custom** (code-packaged LLM-as-a-judge), deployed via `08-create-evaluators.sh` |

> [!TIP]
> `10-optimize-prompt.sh` is a **manual** optimize-and-re-evaluate loop. AgentCore **Optimization**
> (public preview) productizes the same idea — Recommendations, versioned Configuration bundles, and
> A/B testing on top of AgentCore Evaluations. The manual loop here is the conceptual primitive behind it.

---

## 6. Prerequisites

### Where this runs

This customized release runs **only in the Workshop environment your facilitator prepared for this
scenario** with Workshop Customizer (one environment per scenario). It does not run from an unzipped copy
in another account. The prepared environment provides:

| Requirement | Why the scripts need it |
|-------------|-------------------------|
| An EC2 work environment in **`us-west-2`**, created by the `workshop-infra` stack and reached through SSM | Every script runs there; step 2 finds the stack and only prints its outputs |
| The **`workshop-customizer-addons`** CloudFormation stack in the same account | Step 6 (`04-deploy.sh`) reads its `SkillsFilesAccessPointArn` output and stops without it; `99-cleanup.sh` deletes it |
| The Workshop Customizer host helpers under `/opt/workshop-customizer/`, with this release applied as the **active** release | Step 7 (`05-setup-memory.sh`) runs `/opt/workshop-customizer/runtime_permissions.py`, which refuses a release that is not active |
| The release root **`~/workshop/current`** on that EC2 | It holds `RELEASE.json`, this README and the scripts; run everything from there |

If any of these is missing, stop and ask your facilitator — do not deploy the stacks yourself.

### Tools

| Requirement | Notes |
|-------------|-------|
| AWS account | The account of the prepared Workshop environment, with the EC2 instance in **`us-west-2`** to run from |
| AWS CLI | Configured with credentials (`aws sts get-caller-identity` must succeed) |
| Node.js | v20+ |
| Python | 3.10+ (with `pip`) |
| AgentCore CLI | `npm i -g @aws/agentcore@preview` |

### IAM permissions

The identity you run as (the EC2 instance role of the prepared environment) needs permissions to create
and manage these services.

> [!WARNING]
> **A read-only or narrowly scoped role will fail.** The scripts touch:
> `cloudformation`, `ec2` (VPC/subnets/NAT/SG), `s3` + `s3vectors`, `iam` (create/attach roles &
> policies), `bedrock` + `bedrock-agent` + `bedrock-agentcore-control`, `lambda`, `ssm`, `logs`,
> `xray`, `application-signals`, `sts`.

Your facilitator set up the instance role when preparing the environment; do not widen it yourself.

### Region

Set your region **once** in the shell you run everything from, in the release root `~/workshop/current`
(all scripts default to `us-west-2`):

```bash
export AWS_DEFAULT_REGION=us-west-2
cd static/scripts
chmod +x *.sh
```

`static/scripts/` holds one small wrapper per script that runs the release-root script of the same name
with the same arguments.

{{note_prerequisites}}

---

## 7. Execution order at a glance

Approximate timings are the template's end-to-end run on a blank account (us-west-2).
Total ≈ **25–30 minutes** of mostly-unattended waiting.

{{execution_table}}

{{timing_footnote}}

---

## 8. Step-by-step

### Step 1 — `00-setup.sh`  ·  _Phase 0_
Verifies `agentcore`, `node`, and `aws` are installed, prints your account/region, and creates the
Skill directories {{skill_dirs}}.

```bash
./00-setup.sh
```

You should see your account id and region printed.

{{note_setup}}

### Step 2 — `00-deploy-infra.sh`  ·  _Phase 0 — required_
Deploys the `workshop-infra` CloudFormation stack: VPC, private subnets, NAT, security group, the
**data** S3 bucket + Access Point, and an EC2 work environment (reachable via SSM). The template
auto-selects AZs supported by AgentCore. Takes ~5–8 minutes on a blank account.

```bash
./00-deploy-infra.sh
```

> [!CAUTION]
> **Do not skip this.** Later steps depend on this stack's outputs:
> - `01-create-kb.sh` reads the **`DataBucketName`** output to know where to put the Knowledge Base
>   data source — it will **fail** if the stack doesn't exist.
> - `04-deploy.sh` uses the VPC/subnets/SG outputs to deploy the Harness in VPC network mode.
>
> The script is idempotent: if the `workshop-infra` stack already exists — as it does in the prepared
> environment — it skips creation and just prints the outputs.

{{note_infra}}

### Step 3 — `01-create-kb.sh`  ·  _Phase 0_
Copies the scenario's {{doc_count}} knowledge documents from `pack/knowledge-base/docs/` (the docs
directory is emptied first, so no document of an earlier scenario is ingested), then creates the Amazon
Bedrock Knowledge Base `{{kb_name}}` backed by **Amazon S3 Vectors** (embedding model
`amazon.titan-embed-text-v2:0`, 1024 dims), removes stale objects under its S3 prefix, ingests the docs,
and stores the KB ID in SSM at `{{ssm_prefix}}/knowledge_base_id`. The Lambda in the next step reads it
from there — **no manual environment variables needed.**

The documents of this scenario:

{{documents_table}}

> [!NOTE]
> {{noise_sentence}} The models referenced (`amazon.titan-embed-text-v2:0`, `us.amazon.nova-2-lite-v1:0`)
> are invoked as managed Amazon Bedrock models — no model weights are included or distributed.

```bash
./01-create-kb.sh
```

It prints the full KB details (ID, data location, vector store, embedding model) on completion. The
data bucket name is read automatically from the `workshop-infra` stack output `DataBucketName` — **so
step 2 must have completed first**, otherwise this script aborts with
`Stack 'workshop-infra' has no DataBucketName/SkillsBucketName output — is workshop-infra deployed?`

> [!TIP]
> **Cost note:** Amazon S3 Vectors is billed on storage + queries (no always-on cluster), so it is much
> cheaper than an always-on vector DB — but **still delete it when done** (see cleanup).

{{note_knowledge_base}}

### Step 4 — `02-create-gateway.sh`  ·  _Phase 2_
Two things in one step:
1. Packages and deploys the scenario tools **Lambda** (`{{lambda_name}}`: the generic handler plus this
   scenario's `fixtures.json`) and its IAM role (with permission to read the KB ID from SSM and query
   the Knowledge Base).
2. Creates the **Gateway** `{{gateway_name}}` (MCP protocol, AWS_IAM auth) with the Lambda as its
   target `{{target_name}}`, via `gateway/create_gateway.py`. The Gateway ARN is written to SSM at
   `{{ssm_prefix}}/gateway_arn`.

```bash
./02-create-gateway.sh
```

The Gateway exposes {{tool_count}} tools:

{{tools_table}}

Traces name the tools `{{compact_target}}___<tool>` (the target name without hyphens).

{{note_gateway}}

### Step 5 — `03-configure-skills.sh`  ·  _Phase 2_
{{skills_sentence}} They get mounted into the Harness in the next step (BYO Filesystem).

```bash
./03-configure-skills.sh
```

{{note_skills}}

### Step 6 — `04-deploy.sh`  ·  _Phase 2_
Creates the Harness project `{{agent_name}}` and the Memory `{{memory_name}}` (semantic + user-preference
strategies), attaches the **existing** Gateway by ARN (so no duplicate Gateway is created — this is what
makes deployment **single-pass**), installs the baseline system prompt from `pack/prompts/baseline.md`,
restricts `allowedTools` to `@{{target_name}}/*`, mounts the Skills filesystem through the
`workshop-customizer-addons` access point, and deploys.

```bash
./04-deploy.sh
```

> [!NOTE]
> **Network mode:** with the `workshop-infra` stack in place (step 2), the Harness deploys in **VPC**
> mode using that stack's subnets/SG.

{{note_agent}}

### Step 7 — `05-setup-memory.sh`  ·  _Phase 2_
Prepares the runtime for the active release (`/opt/workshop-customizer/runtime_permissions.py`), then
configures Memory **retrieval** on the deployed Harness so every invoke automatically pulls the user's
preferences (`/users/{actorId}/preferences`, top 20) and facts (`/users/{actorId}/facts`, top 10) from
Memory and injects them into context.

```bash
./05-setup-memory.sh
```

> [!NOTE]
> `04-deploy.sh` already *created* `{{memory_name}}`. This step wires up automatic *retrieval* per invoke
> — they are not the same thing.

{{note_memory}}

### Step 8 — `06-test-conversation.sh`  ·  _Phase 3_
Runs the first conversation with a fresh session ID and `actor-id {{first_actor}}`, asking:

{{first_query}}

The answer is intentionally generic at this point — the Agent doesn't know anything about you yet. The
script announces the question and ends with this notice:

{{first_console}}

{{memory_lesson}}

This also produces the first **trace**.

```bash
./06-test-conversation.sh
```

{{note_conversation}}

---

### Step 9 — `07-setup-eval-env.sh`  ·  _Phase 4 pre_
Installs `uv` (required to package evaluator Python dependencies) and enables **CloudWatch Transaction
Search**, so the Agent's OTel trace spans land in CloudWatch where the evaluation service can read them.

```bash
./07-setup-eval-env.sh
```

> [!TIP]
> If the script tells you to, add `uv` to your PATH:
> ```bash
> export PATH="$HOME/.local/bin:$PATH"
> ```

{{note_eval_env}}

### Step 10 — `06-test-conversation.sh` *(run again)*  ·  _Phase 4_
Transaction Search only captures spans created **after** it was enabled. Re-run the conversation to
generate a trace the evaluators can read (the same question, asked as the same actor `{{first_actor}}`):

```bash
./06-test-conversation.sh
```

{{note_conversation_rerun}}

### Step 11 — `08-create-evaluators.sh`  ·  _Phase 4_
Registers and deploys the two custom code-based evaluators, then grants their execution roles Bedrock
invoke permission (needed for the LLM-judge):

- `{{thelma_evaluator}}` — **TRACE** level, RAG quality (7 scores; see the metric table above), primary score = **Groundedness**
- `{{mtg_evaluator}}` — **SESSION** level, **Goal Success Rate (GSR)** + failure attribution (**RCOF**), judged with this scenario's rules

See [`evaluators/README.md`](evaluators/README.md) for what each metric means.

```bash
./08-create-evaluators.sh
```

{{note_evaluators}}

### Step 12 — `09-run-eval.sh`  ·  _Phase 4_
By default, asks the **{{total_questions}} practice questions** below, each as a **fresh Memory actor**
(`<persona>-<phase>-<epoch>-q<i>`, so no answer reuses Memory from another question or run), waits until
the **{{probe_count}} retrieval traces** are indexed, then evaluates and prints, for each: the **Query**,
a truncated **Response**, and the **score** — THELMA 7-score breakdown + diagnosis on the retrieval
probes, Mind the Goal GSR + RCOF on every question, and an L1 table of every question at the end. Each run
keeps a record under `~/workshop/eval-runs/{{agent_name}}/<phase>-<epoch>/`.

{{practice_table}}

```bash
./09-run-eval.sh                        # ask the {{total_questions}} practice questions, then evaluate them (THELMA, Mind the Goal, L1)
./09-run-eval.sh --eval-only [N]        # skip conversations; evaluate the N most recent retrieval traces (default {{probe_count}})
./09-run-eval.sh <trace-id>             # THELMA only, on one trace
./09-run-eval.sh <session-id> session   # Mind the Goal only, on one session
```

#### Reading the output

Each evaluated question prints three lines, then the evaluator's explanation. A THELMA trace:

```text
  Query:    <the practice question>
  Response: <the first 300 characters of the answer> …[截断]
  Score:    trace=<16 hex> value=0.62 [Fail] case=<case id>
     THELMA 7 维 (query: …): GR(接地/防幻觉)=0.62 | SP1(块级检索精度)=0.40 | SP2(事实级检索精度)=0.20 | SQC(源覆盖)=0.75 | RP(响应精度)=0.55 | RQC(响应覆盖)=0.60 | SD(去重)=0.80. 诊断: SP↓ SQC↑->Retriever
```

The Chinese tags are printed by the template's evaluator; `value` is GR, and `[Pass]` means GR ≥ {{gr_pass}}.

{{thelma_legend}}

{{diagnosis_levels}}

A Mind the Goal session:

```text
  Score:    trace=? value=1 [Pass] case=<case id>
     Mind the Goal: GSR=100.0% (1/1 目标达成), 轮次数=1. 失败归因: 无失败
```

{{mtg_reading}}

{{rcof_table}}

The L1 table closes the run: one row per practice question with its L1 status, the failing check, GR and
the Mind the Goal label, then a summary line (`L1: x/y pass, …`).

{{l1_statuses}}

{{l1_compare}}

Progress and retry messages (the progress lines are printed in Chinese by the template's scripts):

{{retry_messages}}

What to look for in this scenario:

{{what_to_look_for}}

{{note_baseline}}

### Step 13 — `10-optimize-prompt.sh`  ·  _Phase 5_
Closes the ADLC loop. Acting on the Phase 4 diagnosis (`SQC↓ RQC↑ GR↓` / `RP↓` → Prompt), it:
1. installs this scenario's optimized System Prompt from `pack/prompts/optimization-candidate.md`
   (compare it with [`pack/prompts/baseline.md`](pack/prompts/baseline.md) — the candidate adds
   **anti-hallucination constraints**),
2. updates the deployed Harness's System Prompt in place (`update-harness`, about 30 s — no redeploy; Memory,
   tools and skills stay as 04/05 set them),
3. re-asks the **same {{total_questions}} practice questions**, each as a fresh optimized-run actor, and
4. re-evaluates the {{probe_count}} new retrieval traces (`09-run-eval.sh --eval-only {{probe_count}}`),
   with the L1 table compared against the baseline run.

```bash
./10-optimize-prompt.sh
```

{{contrast_paragraph}}

At the end the script prints its own reading of the contrast:

{{closing_lines}}

{{retrieval_contrast}}

> [!NOTE]
> Write the optimized prompt in the language of the knowledge documents: with the prompt language
> aligned to the knowledge base, the anti-hallucination constraints land most effectively. LLM-as-judge
> scores fluctuate between runs — read the trend and the diagnosis, not a single absolute number. Your
> facilitator measures the noise band with `13-judge-stability.sh`; a change inside it is not an
> improvement, and if 09 and 10 scored a different number of retrieval traces the mean difference is
> descriptive only.

{{note_optimize}}

---

## 9. What you should see

The teaching moments of this scenario and what each should show. Numbers come from the evaluators;
the thresholds are the workshop's fixed bars.

{{expectations_table}}

{{noise_band_note}}

---

## 10. Optional labs

These three are **optional extensions** beyond the ~2-hour core path. They reuse the Agent and
evaluators you already deployed, so **run them before `99-cleanup.sh`** — once cleanup runs, those
resources are gone.

### `11-cost-latency.sh` — operational metrics (cost & latency)  ·  _Phase 6_
The opening promise of a decision-first agent is three dimensions: **answers well / answers fast /
offloads work**. THELMA already quantified *"answers well."* This script delivers the other two — **without
creating any resources**. It reads the **same traces** you already produced from CloudWatch `aws/spans`
(the same log group as `09-run-eval.sh`), and for the **most recent {{recent_n}} retrieval traces**
computes **end-to-end latency** (max span end − min span start), **input/output tokens** (from
`gen_ai.usage.*` span attributes, counted once across the span hierarchy), and **cost** (tokens × model
unit price). The result is the CXO scorecard: quality (GR) + speed (latency) + cost ($) side by side.

```bash
./11-cost-latency.sh            # latency + token + cost for the most recent {{recent_n}} retrieval traces
./11-cost-latency.sh <trace-id> # just one trace
```

> [!TIP]
> Prices (`PRICE_IN` / `PRICE_OUT`, $ per 1M tokens) default to a verified snapshot for Nova 2 Lite,
> Nova Pro and Claude Haiku 4.5 in `us-west-2`. For any other model or region the script stops with
> `Set verified PRICE_IN and PRICE_OUT …`: export both, confirmed against the AWS pricing page.

{{note_cost_latency}}

### Optional lab A — `12-compare-models.sh` (multi-model comparison)
Answers the question every CXO asks: *"can we switch to a cheaper/faster model and still be good
enough?"* It **non-destructively** switches the deployed Harness to the comparison model in place
(`update-harness`, about 30 s, no redeploy), re-asks the same
{{total_questions}} practice questions (fresh comparison-run actors), scores them with the same THELMA,
compares quality/cost/latency against the Phase 4 baseline, then **restores the baseline model**. Turns
"switch the model" from a gut call into a data-backed decision.

```bash
./12-compare-models.sh                          # default comparison model = Nova Pro
./12-compare-models.sh us.amazon.nova-pro-v1:0  # explicit default
./12-compare-models.sh us.anthropic.claude-haiku-4-5-20251001-v1:0  # try another family
```

> [!WARNING]
> It switches the model **on the deployed Harness** and switches it back on exit — `harness.json` keeps the
> baseline model, and it never re-runs `04-deploy.sh` (which would `rm -rf {{agent_name}}` and delete your evaluators). Avoid the **Nova Micro** tier as the
> comparison model: under the Strands strict ToolUse protocol it often errors with
> `Model produced invalid sequence as part of ToolUse`, so the conversations fail and you get no data.
> That instability is itself a useful evaluation finding — *the model is incompatible with your current
> agent topology* — but it doesn't make a good first demo.

{{note_models}}

### Optional lab B — `13-judge-stability.sh` (judge stability)
Answers the follow-up every CXO asks: *"is your AI judge (THELMA) itself reliable, or does it score
randomly?"* A lightweight **repeatability** check: it scores the **same trace** N times and looks at the
spread — consistent scores mean a trustworthy judge; scores bouncing around mean treat the conclusions
with caution (small models are especially prone to this). By default it takes the most recent retrieval
trace, which after 10 or 12 is the question asked last: {{stability_case}}.

```bash
./13-judge-stability.sh                # most recent retrieval trace, scored {{stability_runs}} times
./13-judge-stability.sh <trace-id> [N] # a specific trace, N times (default {{stability_runs}})
```

> [!NOTE]
> The script judges the spread by the standard deviation: ≤ {{stable_std}} `稳定` (stable),
> ≤ {{moderate_std}} `一般` (moderate), otherwise `不稳` (unstable); fewer than two usable scores print
> `数据不足`. The judge defaults to **Nova 2 Lite** (a small model), so the spread may be wider — which is
> exactly what this lab surfaces. Your facilitator turns this spread into the noise band of §9.
> Repeatability is only one lightweight check; the production-recommended complement is
> **human-sample calibration** (TPR/TNR against a labeled set).

{{note_judge_stability}}

{{student_experiments}}

---

## 11. Cleanup — `99-cleanup.sh`

> [!CAUTION]
> **Run this only when your facilitator tells you to.** It deletes the whole Workshop environment of
> this scenario to avoid ongoing charges (Knowledge Base, Lambdas, NAT gateway, etc.).

```bash
./99-cleanup.sh
```

The script tears down the agent `{{agent_name}}`, the Gateway `{{gateway_name}}`, the Lambda
`{{lambda_name}}`, the Knowledge Base `{{kb_name}}`, the SSM parameters under `{{ssm_prefix}}`, the run
records under `~/workshop/eval-runs/{{agent_name}}/`, the Customizer add-on stack (before
`workshop-infra`), and finally the base stack. If AgentCore service-managed ENIs are still attached it
prints `Cleanup pending` and exits with status 75: they can take up to 8 hours to release — re-run the
same command later. It never reports success while resources are retained. The script is idempotent —
re-running is safe.

{{note_cleanup}}

---

## 12. Data sources & attribution

{{attribution_block}}

The two custom evaluators (THELMA, Mind the Goal) are independent re-implementations of published
research methods — see [`evaluators/README.md`](evaluators/README.md) for their citations and licensing.
The models referenced are invoked as managed Amazon Bedrock models; no model weights are included or
distributed.

---

## Security

See CONTRIBUTING in `aws-samples/sample-eval-first-building-enterprise-agents-with-agentcore` at commit
`{{template_commit_short}}` for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
{{supplement}}
