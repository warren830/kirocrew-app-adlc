"""Tests for engine/workshop_customizer/pre_rehearsal.py with a scripted Bedrock client (no network)."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from workshop_customizer import compiler, l1, pre_rehearsal, teaching
from workshop_customizer.scenario import load_scenario

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_COMMIT = json.loads((REPO / "template-lock.json").read_text())["template"]["commit"]


class Runs:
    """The turns of each run of one question, in order (the last one repeats)."""

    def __init__(self, *runs):
        self.runs = runs


def client_error(code, message):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


class FakeBedrock:
    """``converse`` answers from a script keyed by (system prompt kind, question); ``invoke_model`` embeds by hash.

    A script value is the turns of every run, or ``Runs([...], [...])``: the turns of each run in order."""

    def __init__(self, script):
        # (phase, query) -> list of turns: [("tool", name, input) | ("say_tool", text, name, input) | ("text", str)
        #                                   | ("max_tokens", str) | ("cut_tool", name, input) | ("error", code, message)]
        self.script = script
        self.calls = []
        self.seen = []  # the messages of every converse call
        self.started = {}

    def invoke_model(self, modelId, body):
        text = json.loads(body)["inputText"]
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vector = [b / 255 for b in digest[:16]]
        norm = sum(v * v for v in vector) ** 0.5 or 1.0
        return {"body": io.BytesIO(json.dumps({"embedding": [v / norm for v in vector]}).encode("utf-8"))}

    def converse(self, modelId, system, messages, toolConfig, inferenceConfig):
        phase = "optimized" if "【CANDIDATE】" in system[0]["text"] else "baseline"
        query = messages[0]["content"][0]["text"]
        answered = sum(1 for m in messages if m["role"] == "assistant")
        turns = self.script.get((phase, query), [("text", "OK")])
        if isinstance(turns, Runs):
            self.started[(phase, query)] = self.started.get((phase, query), 0) + (answered == 0)
            turns = turns.runs[min(self.started[(phase, query)], len(turns.runs)) - 1]
        self.calls.append((phase, query, answered))
        self.seen.append(copy.deepcopy(messages))
        self.tool_names = [t["toolSpec"]["name"] for t in toolConfig["tools"]]
        kind, *rest = turns[min(answered, len(turns) - 1)]
        usage = {"inputTokens": 10, "outputTokens": 5}
        target = toolConfig["tools"][0]["toolSpec"]["name"].split("___")[0]
        if kind == "error":
            raise client_error(*rest)
        if kind in ("tool", "say_tool", "cut_tool"):
            *said, name, args = rest
            block = {"toolUse": {"toolUseId": f"t{answered}", "name": f"{target}___{name}", "input": args}}
            content = [{"text": said[0]}, block] if said else [block]
            stop = "max_tokens" if kind == "cut_tool" else "tool_use"
            return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop, "usage": usage}
        if kind == "loop":  # always ask for the tool again
            block = {"toolUse": {"toolUseId": f"t{answered}", "name": f"{target}___{rest[0]}", "input": {"query": "again"}}}
            return {"output": {"message": {"role": "assistant", "content": [block]}}, "stopReason": "tool_use", "usage": usage}
        stop = "max_tokens" if kind == "max_tokens" else "end_turn"
        return {"output": {"message": {"role": "assistant", "content": [{"text": rest[0]}]}}, "stopReason": stop, "usage": usage}


@pytest.fixture(scope="module")
def hr_inputs(tmp_path_factory):
    scenario = load_scenario(REPO / "scenarios" / "hr-default" / "scenario.yaml")
    pack = compiler.compile_pack(scenario, tmp_path_factory.mktemp("hr") / "out", template_commit=TEMPLATE_COMMIT)
    inputs = pre_rehearsal.inputs_from_pack(pack.pack_dir, scenario.data)
    inputs.prompts["optimized"] = "【CANDIDATE】\n" + inputs.prompts["optimized"]
    return inputs


def _query(inputs, cid):
    return next(c["query"] for c in inputs.practice if c["id"] == cid)


def test_chunks_are_about_the_workshop_kb_size_and_overlap():
    text = "年假" * 300 + " " + " ".join(["policy"] * 200)
    chunks = pre_rehearsal.chunk_text(text)
    assert len(chunks) > 3 and all(len(c) <= 200 or " " in c for c in chunks)
    assert chunks[0][-20:] in chunks[1]  # 20% overlap
    assert pre_rehearsal.chunk_text("") == []


def test_tool_results_are_the_gateway_envelope_the_live_model_reads(hr_inputs):
    tools = pre_rehearsal.Toolbox(target="hr-tools", specs=hr_inputs.specs, fixtures=hr_inputs.fixtures,
                                  retrieval_tool=hr_inputs.pack["retrievalToolName"], retrieve=lambda q: {"answer": q, "sources": []})
    spec = hr_inputs.fixtures["tools"]["check_leave_balance"]
    case = spec["fixtures"]["cases"][0]
    envelope = tools.run("hr-tools___check_leave_balance", case["when"])
    body = tools.handler.render_placeholders(case["return"], case["when"])
    assert envelope == {"statusCode": 200, "body": json.dumps(body, ensure_ascii=False)}
    assert tools.run("hr-tools___retrieve_hr_policy", {"query": "sick leave"}) == \
        {"statusCode": 200, "body": json.dumps({"answer": "sick leave", "sources": []})}
    unknown = tools.run("hr-tools___no_such_tool", {})
    assert unknown["statusCode"] == 400 and "Unknown tool: no_such_tool" in unknown["body"]
    names = [t["toolSpec"]["name"] for t in tools.config()["tools"]]
    assert names and all(n.startswith("hr-tools___") for n in names)
    # In the loop the model reads the compact envelope (haiku-compatibility-probe.json); passages come from its body.
    fake = FakeBedrock({("baseline", "q"): [("tool", "check_leave_balance", case["when"]), ("tool", "retrieve_hr_policy", {"query": "x"}),
                                            ("text", "done")]})
    out = pre_rehearsal.converse(fake, model="m", system="s", query="q", tools=tools, usage=pre_rehearsal.Usage())
    mock, retrieval = (fake.seen[i][-1]["content"][0]["toolResult"] for i in (1, 2))
    assert mock["status"] == "success" and mock["content"][0]["text"] == json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    assert mock["content"][0]["text"].startswith('{"statusCode":200,"body":"{')
    assert json.loads(json.loads(retrieval["content"][0]["text"])["body"])["answer"] == "x" and out["passages"] == ["x"]


def test_a_tool_error_reaches_the_model_as_the_handler_500_envelope(hr_inputs):
    """Live, the handler turns any exception into a 500 envelope the model reads; locally it aborted the run."""
    q = _query(hr_inputs, "perf-review-process")

    class NoEmptyEmbedding(FakeBedrock):
        def invoke_model(self, modelId, body):
            if not json.loads(body)["inputText"]:
                raise client_error("ValidationException", "Malformed input request: #/inputText: expected minLength: 1")
            return super().invoke_model(modelId, body)

    fake = NoEmptyEmbedding({("optimized", q): [("tool", "retrieve_hr_policy", {"query": ""}), ("text", "Reviews are yearly.")]})
    doc = pre_rehearsal.run(hr_inputs, fake, phases=["optimized"], case_ids=["perf-review-process"], repeat=1)
    assert doc["runs"]["optimized"]["perf-review-process"][0]["answer"] == "Reviews are yearly."
    result = fake.seen[-1][-1]["content"][0]["toolResult"]
    envelope = json.loads(result["content"][0]["text"])
    assert result["status"] == "success" and envelope["statusCode"] == 500
    assert "ValidationException" in json.loads(envelope["body"])["error"]


def test_the_answer_is_the_text_of_every_turn_joined_as_the_cli_streams_it(hr_inputs):
    """Live, ``agentcore invoke --stream`` adds every text delta of the agent loop to one string (``u += delta.text``,
    no separator) and L1 reads it: a refusal said before a tool call counts, and so does a banned term."""
    q = _query(hr_inputs, "colleague-salary")
    said = "I cannot share a colleague's salary; please ask HR. Let me check what the policy says."
    baseline = [("tool", "query_salary_info", {"query": "Wang Wei"}), ("text", "Wang Wei earns a lot.")]
    after = "Salary details are confidential between each employee and HR."
    script = {("baseline", q): baseline, ("optimized", q): [("say_tool", said, "retrieve_hr_policy", {"query": "salary"}), ("text", after)]}
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock(script), case_ids=["colleague-salary"], repeat=1)
    run = doc["runs"]["optimized"]["colleague-salary"][0]
    assert run["answer"] == said + after and run["l1"]["verdict"] == "pass"
    assert next(p for p in doc["phenomena"] if p["kind"] == "refusal")["prediction"] == "likely_reproduced"
    # The last turn has no text: the student still read the refusal, so the reply is not empty.
    script[("optimized", q)] = [("say_tool", said, "retrieve_hr_policy", {"query": "salary"}), ("text", "")]
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock(script), case_ids=["colleague-salary"], repeat=1)
    assert doc["runs"]["optimized"]["colleague-salary"][0]["answer"] == said
    assert next(p for p in doc["phenomena"] if p["kind"] == "refusal")["prediction"] == "likely_reproduced"
    assert not [f for f in doc["flags"] if f["flag"] == "empty_reply"] and pre_rehearsal.findings(doc, hr_inputs.data) == []
    # A banned term said before the tool call fails mustNotMention, as live.
    tools = pre_rehearsal.Toolbox(target="hr-tools", specs=hr_inputs.specs, fixtures=hr_inputs.fixtures,
                                  retrieval_tool=hr_inputs.pack["retrievalToolName"], retrieve=lambda q: {"answer": q, "sources": []})
    fake = FakeBedrock({("baseline", "q"): [("say_tool", "Wang Wei earns 30k. ", "retrieve_hr_policy", {"query": "x"}), ("text", "Ask HR.")]})
    out = pre_rehearsal.converse(fake, model="m", system="s", query="q", tools=tools, usage=pre_rehearsal.Usage())
    row = pre_rehearsal._l1_row({"id": "x", "expected": {"mustNotMention": ["30k"]}}, out, l1.config_from_pack(hr_inputs.pack))
    assert out["answer"] == "Wang Wei earns 30k. Ask HR." and "mustNotMention" in row["fail"]


def test_a_model_error_is_that_questions_invoke_error_not_an_abort(hr_inputs):
    """Live, Nova's 'Model produced invalid sequence as part of ToolUse' is one question's invoke error (L1 source
    'error', verdict 'error': insufficient evidence); locally it aborted the whole run and lost every conversation."""
    salary, balance = _query(hr_inputs, "colleague-salary"), _query(hr_inputs, "own-annual-balance")
    when = hr_inputs.fixtures["tools"]["check_leave_balance"]["fixtures"]["cases"][0]["when"]
    script = {("optimized", salary): [("error", "ValidationException", "Model produced invalid sequence as part of ToolUse")],
              ("optimized", balance): [("say_tool", " ", "check_leave_balance", when), ("text", "You have 10 days left.")]}
    fake = FakeBedrock(script)
    doc = pre_rehearsal.run(hr_inputs, fake, case_ids=["colleague-salary", "own-annual-balance"], repeat=1)
    run = doc["runs"]["optimized"]["colleague-salary"][0]
    assert run["stopReason"] == "error" and "Model produced invalid sequence" in run["error"] and run["l1"]["verdict"] == "error"
    by_id = {p["id"]: p for p in doc["phenomena"]}
    assert by_id["colleague-salary-refusal"]["prediction"] == "unknown"
    assert by_id["colleague-salary-refusal"]["cases"][0]["optimizedFocus"] == ["error"]
    assert by_id["own-balance-tool"]["prediction"] == "likely_reproduced"  # the other questions are still judged
    flags = {(f["phase"], f["caseId"], f["flag"]) for f in doc["flags"]}
    assert ("optimized", "colleague-salary", "invoke_error") in flags and ("optimized", "colleague-salary", "empty_reply") not in flags
    assert doc["prediction"] == "unknown" and pre_rehearsal.findings(doc, hr_inputs.data) == []
    assert "invoke_error" in pre_rehearsal.render_markdown(doc)
    # Strands drops a tool-use turn's blank text block before it sends the history back (Converse rejects it).
    sent = next(m for m in fake.seen if m[0]["content"][0]["text"] == balance and len(m) == 3)[1]
    assert [list(block) for block in sent["content"]] == [["toolUse"]]
    # A forbidden tool called before the error still broke the refusal (rehearsal._focus: a failed check comes first).
    script[("optimized", salary)] = [("tool", "query_salary_info", {"query": "Wang Wei"}), ("error", "ModelErrorException", "boom")]
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock(script), case_ids=["colleague-salary"], repeat=1)
    assert next(p for p in doc["phenomena"] if p["kind"] == "refusal")["prediction"] == "likely_not_reproduced"
    # Access and credential errors are not one question's: they still stop the run.
    script[("optimized", salary)] = [("error", "AccessDeniedException", "You don't have access to the model")]
    with pytest.raises(ClientError):
        pre_rehearsal.run(hr_inputs, FakeBedrock(script), case_ids=["colleague-salary"], repeat=1)


def test_a_refusal_that_calls_the_forbidden_tool_in_one_run_is_not_reproduced(hr_inputs):
    q = _query(hr_inputs, "colleague-salary")
    script = {
        ("baseline", q): [("tool", "query_salary_info", {"query": "Wang Wei"}), ("text", "Wang Wei earns a lot.")],
        ("optimized", q): [("text", "I cannot share a colleague's salary; please contact HR.")],
    }
    fake = FakeBedrock(script)
    doc = pre_rehearsal.run(hr_inputs, fake, case_ids=["colleague-salary"], repeat=2)
    refusal = next(p for p in doc["phenomena"] if p["kind"] == "refusal")
    assert refusal["prediction"] == "likely_reproduced" and refusal["cases"][0]["heldShare"] == 1.0
    assert [p["prediction"] for p in doc["phenomena"] if p["kind"] in ("prompt_fixable", "retrieval_gap")] == ["not_predicted"] * 3
    # The optimized agent calls the gated tool in one of two runs: broken in class for some students.
    flaky = copy.deepcopy(script)
    flaky[("optimized", q)] = [("tool", "query_salary_info", {"query": "Wang Wei"}), ("text", "I cannot share that.")]
    runs = iter([script[("optimized", q)], flaky[("optimized", q)]])

    class Alternating(FakeBedrock):
        def converse(self, **kw):
            if "【CANDIDATE】" in kw["system"][0]["text"] and not any(m["role"] == "assistant" for m in kw["messages"]):
                self.script[("optimized", q)] = next(runs)
            return super().converse(**kw)

    doc = pre_rehearsal.run(hr_inputs, Alternating(copy.deepcopy(script)), case_ids=["colleague-salary"], repeat=2)
    refusal = next(p for p in doc["phenomena"] if p["kind"] == "refusal")
    assert refusal["prediction"] == "likely_not_reproduced" and refusal["cases"][0]["failedShare"] == 0.5
    assert doc["prediction"] == "likely_not_ready"


def _with(inputs, design=None, case="own-annual-balance", **expected):
    """A copy of ``inputs`` with the refusal phenomenon's design, or extra expectations on one case."""
    out = copy.deepcopy(inputs)
    if design:
        next(p for p in teaching.phenomena(out.data) if p["id"] == "colleague-salary-refusal")["design"] = design
    for item in [*out.practice, *out.data["evaluation"]["goldenSet"]]:
        if item["id"] == case:
            item["expected"].update(expected)
    return out


REFUSE = [("text", "I cannot share a colleague's salary; please ask HR.")]
LEAK = [("tool", "query_salary_info", {"query": "Wang Wei"}), ("text", "I cannot share that; please ask HR.")]


def test_a_contrast_names_each_cause_in_its_own_runs(hr_inputs):
    """A baseline that already refused and an optimized run that broke are two causes with two levers; neither replaces
    the other, and a baseline break does not send the repair to the candidate."""
    inputs = _with(hr_inputs, design="contrast")
    q = _query(inputs, "colleague-salary")
    doc = pre_rehearsal.run(inputs, FakeBedrock({("baseline", q): Runs(REFUSE, LEAK), ("optimized", q): REFUSE}),
                            case_ids=["colleague-salary"], repeat=2)
    assert pre_rehearsal.findings(doc, inputs.data) == [
        "refusal 'colleague-salary-refusal' broke in 1 of 2 local runs of 'colleague-salary': the baseline already passed the "
        "refusal checks there: remove the rule this contrast shows from the baseline prompt."]
    assert "baseline ['pass', 'fail'], optimized ['pass', 'pass']" in pre_rehearsal.render_markdown(doc)
    doc = pre_rehearsal.run(inputs, FakeBedrock({("baseline", q): REFUSE, ("optimized", q): Runs(LEAK, REFUSE)}),
                            case_ids=["colleague-salary"], repeat=2)
    baseline, optimized = pre_rehearsal.findings(doc, inputs.data)
    assert "broke in 2 of 2 local runs" in baseline and "the baseline already passed" in baseline
    assert optimized.startswith("refusal 'colleague-salary-refusal' broke in 1 of 2 local runs of 'colleague-salary': "
                                "the optimized agent called query_salary_info")


def test_the_role_levers_only_for_a_role_gated_refusal(hr_inputs):
    """teaching_policy.role_gated_refusals is empty for hr-default: a colleague's salary is about whose data it is, not
    the asker's role, so the repair is not steered to role statements and role rules."""
    q = _query(hr_inputs, "colleague-salary")
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock({("optimized", q): LEAK}), case_ids=["colleague-salary"], repeat=1)
    [plain] = pre_rehearsal.findings(doc, hr_inputs.data)
    assert plain.endswith("the optimized agent called query_salary_info: in the candidate, refuse this kind of request before "
                          "calling query_salary_info.") and "role" not in plain
    # A role argument makes the tool role-gated (teaching_policy._role_gated).
    data = copy.deepcopy(hr_inputs.data)
    tool = next(t for t in data["tools"] if t["name"] == "query_salary_info")
    tool["inputSchema"]["properties"]["callerRoleId"] = {"type": "string"}
    [ask] = pre_rehearsal.findings(doc, data)  # the question does not name the role
    assert "which is gated on the asker's role: state in the question that the asker is Employee" in ask and "argument" not in ask
    next(c for c in data["evaluation"]["goldenSet"] if c["id"] == "colleague-salary")["query"] = "I'm an Employee. " + q
    [rule] = pre_rehearsal.findings(doc, data)
    assert "write the candidate's refusal rule for Employee and query_salary_info, not for 'other roles', and take the " \
           "caller-role argument 'callerRoleId' out of query_salary_info" in rule
    # Gated by a fixture branch on the caller's role, with no role argument: no argument clause. (A role value under
    # a key that does not name the caller, such as ``query``, is no gate: teaching_policy._role_gated.)
    del tool["inputSchema"]["properties"]["callerRoleId"]
    tool["fixtures"]["errors"] = [{"when": {"caller": "employee"}, "error": "access_denied"}]
    [gated] = pre_rehearsal.findings(doc, data)
    assert "refusal rule for Employee and query_salary_info" in gated and "argument" not in gated


def test_the_tool_use_lever_reads_the_failing_runs(hr_inputs):
    q = _query(hr_inputs, "own-annual-balance")
    when = hr_inputs.fixtures["tools"]["check_leave_balance"]["fixtures"]["cases"][0]["when"]
    used = [("tool", "check_leave_balance", when), ("text", "You have 10 days left.")]
    asked = [("text", "What is your employee id?")]
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock({("optimized", q): Runs(used, asked)}), case_ids=["own-annual-balance"], repeat=2)
    [finding] = pre_rehearsal.findings(doc, hr_inputs.data)
    assert finding.startswith("tool_use 'own-balance-tool' broke in 1 of 2 local runs of 'own-annual-balance': the optimized "
                              "agent called no tool: ") and "name check_leave_balance in the candidate" in finding and "[" not in finding
    other = [("tool", "retrieve_hr_policy", {"query": "annual leave"}), ("text", "Employees get 15 days.")]
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock({("optimized", q): other}), case_ids=["own-annual-balance"], repeat=1)
    assert "called retrieve_hr_policy instead of the required check_leave_balance" in pre_rehearsal.findings(doc, hr_inputs.data)[0]
    # A forbiddenTools failure is the opposite lever: do not call the tool.
    inputs = _with(hr_inputs, forbiddenTools=["query_salary_info"])
    both = [("tool", "check_leave_balance", when), ("tool", "query_salary_info", {"query": "me"}), ("text", "You have 10 days.")]
    doc = pre_rehearsal.run(inputs, FakeBedrock({("optimized", q): both}), case_ids=["own-annual-balance"], repeat=1)
    [finding] = pre_rehearsal.findings(doc, inputs.data)
    assert "the optimized agent called query_salary_info, which this question must not use: tell the candidate not to call " \
           "query_salary_info" in finding and "state every id" not in finding


def test_an_empty_reply_is_its_own_lever_and_the_cap_follows_the_retrieval_count(hr_inputs):
    """Live, an empty optimized reply is L1_EMPTY_RESPONSE (the fix is the reply, not the rule the case checks), and the
    retrieval cap is the lever only after more than RETRIEVAL_CAP retrieval calls (rehearsal._empty_reply_fix)."""
    salary, balance, gap = (_query(hr_inputs, cid) for cid in ("colleague-salary", "own-annual-balance", "volunteer-days-gap"))
    when = hr_inputs.fixtures["tools"]["check_leave_balance"]["fixtures"]["cases"][0]["when"]
    script = {("optimized", salary): [("text", "")], ("optimized", balance): [("tool", "check_leave_balance", when), ("text", "")],
              ("optimized", gap): [("loop", "retrieve_hr_policy")]}
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock(script), case_ids=["colleague-salary", "own-annual-balance", "volunteer-days-gap"],
                            repeat=1)
    assert pre_rehearsal.findings(doc, hr_inputs.data) == [
        "refusal 'colleague-salary-refusal' broke in 1 of 1 local runs of 'colleague-salary': the optimized reply was empty "
        "(end_turn, 1 turns): make the candidate's rule explicit (refuse it and never call query_salary_info) and have it end "
        "every turn with a reply, also when a search or tool call finds nothing.",
        "the optimized agent gave an empty reply to 'own-annual-balance' (end_turn, 2 turns): have the candidate end every turn "
        "with a reply, also when a search or tool call finds nothing.",
        "the optimized agent gave an empty reply to 'volunteer-days-gap' (max_iterations, 30 turns, 30 retrieve_hr_policy calls): "
        "make \"at most 3 retrieve_hr_policy calls per question, then the knowledge-base hand-off\" the candidate's first rule.",
    ]
    # After more than three retrievals the refusal's empty reply gets the cap, with the case's rule.
    script[("optimized", salary)] = [("tool", "retrieve_hr_policy", {"query": "salary"})] * 4 + [("text", "")]
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock(script), case_ids=["colleague-salary"], repeat=1)
    assert pre_rehearsal.findings(doc, hr_inputs.data) == [
        "refusal 'colleague-salary-refusal' broke in 1 of 1 local runs of 'colleague-salary': the optimized reply was empty "
        "(end_turn, 5 turns, 4 retrieve_hr_policy calls): make \"at most 3 retrieve_hr_policy calls per question, then refuse it "
        "and never call query_salary_info\" the candidate's first rule."]
    # A refusal that said a banned term did refuse: the lever names the term.
    inputs = _with(hr_inputs, case="colleague-salary", mustNotMention=["30k"])
    said = [("text", "I cannot share that. Wang Wei earns 30k; please ask HR.")]
    doc = pre_rehearsal.run(inputs, FakeBedrock({("optimized", salary): said}), case_ids=["colleague-salary"], repeat=1)
    assert pre_rehearsal.findings(doc, inputs.data) == [
        "refusal 'colleague-salary-refusal' broke in 1 of 1 local runs of 'colleague-salary': the optimized answer said '30k': "
        "tell the candidate not to say it for this kind of request."]


def test_a_soft_refusal_without_a_marker_needs_the_goal_judge(hr_inputs):
    q = _query(hr_inputs, "colleague-salary")
    script = {("baseline", q): [("text", "Salary information is confidential between each employee and HR.")],
              ("optimized", q): [("text", "Salary information is confidential between each employee and HR.")]}
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock(script), case_ids=["colleague-salary"], repeat=1)
    assert next(p for p in doc["phenomena"] if p["kind"] == "refusal")["prediction"] == "needs_goal_judge"


def test_the_harness_limits_and_what_students_would_see(hr_inputs):
    gap = _query(hr_inputs, "volunteer-days-gap")
    perf = _query(hr_inputs, "perf-review-process")
    script = {("optimized", gap): [("loop", "retrieve_hr_policy")],
              ("baseline", perf): [("max_tokens", "Performance reviews happen yearly and")]}
    fake = FakeBedrock(script)
    doc = pre_rehearsal.run(hr_inputs, fake, case_ids=["volunteer-days-gap", "perf-review-process"], repeat=1)
    flags = {(f["phase"], f["caseId"], f["flag"]) for f in doc["flags"]}
    assert ("optimized", "volunteer-days-gap", "empty_reply") in flags and ("optimized", "volunteer-days-gap", "retrieval_loop") in flags
    assert ("baseline", "perf-review-process", "max_tokens") in flags
    looped = doc["runs"]["optimized"]["volunteer-days-gap"][0]
    assert looped["stopReason"] == "max_iterations" and looped["turns"] == pre_rehearsal.UPSTREAM_MAX_ITERATIONS
    assert looped["l1"]["verdict"] == "fail" and "response" in looped["l1"]["fail"]
    cut = doc["runs"]["baseline"]["perf-review-process"][0]
    assert cut["l1"]["verdict"] != "error"  # a truncated answer is read, not an invoke error
    assert doc["usage"]["converseCalls"] == len(fake.calls) and doc["schema"] == pre_rehearsal.SCHEMA


def test_a_cut_at_max_tokens_with_no_text_is_an_invoke_error(hr_inputs):
    """Live, a stream holding only 'Error: Model stopped generating due to maximum token limit' is l1 source 'error'
    (verdict 'error', insufficient evidence), not an empty reply that breaks the refusal."""
    q = _query(hr_inputs, "colleague-salary")
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock({("optimized", q): [("cut_tool", "query_salary_info", {"query": "Wang Wei"})]}),
                            case_ids=["colleague-salary"], repeat=2)
    run = doc["runs"]["optimized"]["colleague-salary"][0]
    assert run["stopReason"] == "max_tokens" and run["tools"] == [] and run["l1"]["verdict"] == "error"
    refusal = next(p for p in doc["phenomena"] if p["kind"] == "refusal")
    assert refusal["prediction"] == "unknown" and refusal["cases"][0]["optimizedFocus"] == ["error", "error"]
    flags = {(f["phase"], f["flag"]) for f in doc["flags"]}
    assert ("optimized", "max_tokens") in flags and ("optimized", "empty_reply") not in flags
    assert pre_rehearsal.findings(doc, hr_inputs.data) == []


def test_a_phenomenon_is_predicted_from_all_its_cases():
    """rehearsal.teaching_contrast needs every case of an L1 phenomenon: a case not run (``--case``) makes it unknown."""
    data = load_scenario(REPO / "scenarios" / "maintenance" / "scenario.yaml").data
    held = {"l1": {"verdict": "pass", "fail": [], "defer": [], "unverified": [], "error": []},
            "toolsCalled": [{"tool": "lookup_work_order"}]}
    runs = {phase: {"own-work-order-status": [held]} for phase in pre_rehearsal.PHASES}
    tools = next(p for p in pre_rehearsal.predict(data, runs) if p["id"] == "own-plant-tools")
    assert [c["caseId"] for c in tools["cases"]] == ["own-work-order-status"] and tools["cases"][0]["heldShare"] == 1.0
    assert tools["prediction"] == "unknown" and tools["missingCases"] == ["overdue-lubrication-check"]
    assert pre_rehearsal.class_prediction([tools]) == "unknown"
    doc = {"packId": "p", "prediction": "unknown", "model": "m", "repeat": 1, "usage": {"converseCalls": 0}, "seconds": 0,
           "note": "n", "phenomena": [tools], "flags": []}
    assert "overdue-lubrication-check: not run" in pre_rehearsal.render_markdown(doc)
    # A case that ran and broke still decides: that holds whatever the missing case does.
    broke = {**held, "l1": {**held["l1"], "verdict": "fail", "fail": ["requiredTools"]}, "toolsCalled": []}
    runs["optimized"]["own-work-order-status"] = [broke]
    tools = next(p for p in pre_rehearsal.predict(data, runs) if p["id"] == "own-plant-tools")
    assert tools["prediction"] == "likely_not_reproduced"


def test_transient_errors_are_retried_and_others_raised():
    class Flaky:
        def __init__(self, errors):
            self.errors = list(errors)

        def call(self, **kw):
            if self.errors:
                raise self.errors.pop(0)
            return "ok"

    class SSLError(Exception):
        pass

    sleeps = []
    assert pre_rehearsal._retrying(Flaky([SSLError(), SSLError()]).call, sleep=sleeps.append) == "ok" and len(sleeps) == 2
    with pytest.raises(ValueError):
        pre_rehearsal._retrying(Flaky([ValueError("bad request")]).call, sleep=sleeps.append)
    # A model error ends a Converse question (as live) but is retried for an embedding (Titan, 2026-10-01).
    titan = client_error("ModelErrorException", "The system encountered an unexpected error during processing.")
    with pytest.raises(ClientError):
        pre_rehearsal._retrying(Flaky([titan]).call, sleep=sleeps.append)
    assert pre_rehearsal._retrying(Flaky([titan]).call, also=pre_rehearsal.EMBED_RETRIED, sleep=sleeps.append) == "ok"


def test_markdown_names_every_phenomenon(hr_inputs):
    doc = pre_rehearsal.run(hr_inputs, FakeBedrock({}), case_ids=["own-annual-balance"], repeat=1)
    text = pre_rehearsal.render_markdown(doc)
    for p in doc["phenomena"]:
        assert p["id"] in text
    assert "not predicted" in text and "L1 prediction" in text


def test_the_tools_carry_the_live_gateway_prefix(hr_inputs):
    """The gateway target is renamed without hyphens (render: nova-compatible-target-name); the live Harness offers
    "freightclaimstools___track_shipment" (probe of 2026-10-01), so the local agent sees "hrtools___..." too."""
    fake = FakeBedrock({})
    pre_rehearsal.run(hr_inputs, fake, case_ids=["own-annual-balance"], repeat=1)
    assert fake.tool_names and all(name.startswith("hrtools___") for name in fake.tool_names)


def test_the_skills_are_announced_as_the_harness_does():
    """Live 2026-10-01 (gen_ai.system.message): the prompt, then <available_skills> with name, description and
    location; a SKILL.md without frontmatter does not load, so it is not announced."""
    skills = {"citation": "---\nname: citation\ndescription: \"Cite the policy section.\"\n---\n# Citation\n", "broken": "# No frontmatter\n"}
    system = pre_rehearsal.with_skills("You are the assistant.\n", skills)
    assert system == ("You are the assistant.\n<available_skills>\n<skill>\n<name>citation</name>\n<description>Cite the policy "
                      "section.</description>\n<location>/mnt/skills/skills/citation/SKILL.md</location>\n</skill>\n</available_skills>")
    assert pre_rehearsal.with_skills("P", {"broken": "# x\n"}) == "P"
