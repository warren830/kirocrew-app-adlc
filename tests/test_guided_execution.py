import json
import pytest
from workshop_customizer.guided_execution import DOCUMENT_NAME, MALFORMED_OUTPUT, command_request, parse_invocation, poll_step, send_step


class StubSsm:
    def __init__(self):
        self.sent = None
        self.invocation = {"Status": "InProgress"}

    def send_command(self, **kwargs):
        self.sent = kwargs
        return {"Command": {"CommandId": "cmd-123"}}

    def get_command_invocation(self, **kwargs):
        assert kwargs == {"CommandId": "cmd-123", "InstanceId": "i-0123456789abcdef0"}
        return self.invocation


def test_request_is_fixed_document_and_allowlisted_script():
    req = command_request(step_id="baseline", release_version="northstar-abcdef123456", instance_id="i-0123456789abcdef0")
    assert req["DocumentName"] == DOCUMENT_NAME
    assert req["Parameters"] == {"ReleaseVersion": ["northstar-abcdef123456"], "StepId": ["baseline"], "Script": ["09-run-eval.sh"]}


@pytest.mark.parametrize("step,version,instance", [("cleanup", "northstar-abcdef123456", "i-0123456789abcdef0"), ("setup", "bad", "i-0123456789abcdef0"), ("setup", "northstar-abcdef123456", "host")])
def test_request_rejects_unknown_or_malformed_values(step, version, instance):
    with pytest.raises(ValueError):
        command_request(step_id=step, release_version=version, instance_id=instance)


def test_parse_success_and_failure():
    success = parse_invocation({"Status": "Success", "StandardOutputContent": json.dumps({"status": "passed", "summary": "15 cases", "outputs": {"score": 0.83}})})
    assert success == {"passed": True, "summary": "15 cases", "outputs": {"score": 0.83}, "error": "", "documentVersion": None}
    failed = parse_invocation({"Status": "Failed", "StandardErrorContent": "script exit 2"})
    assert failed["passed"] is False and "exit 2" in failed["error"]
    with pytest.raises(ValueError, match="not terminal"):
        parse_invocation({"Status": "InProgress"})


def test_send_and_poll_step_with_stub_ssm():
    ssm = StubSsm()
    assert send_step(ssm, step_id="baseline", release_version="northstar-abcdef123456", instance_id="i-0123456789abcdef0") == "cmd-123"
    assert ssm.sent["DocumentName"] == DOCUMENT_NAME
    assert poll_step(ssm, command_id="cmd-123", instance_id="i-0123456789abcdef0") == {"terminal": False, "status": "InProgress"}
    ssm.invocation = {"Status": "Success", "StandardOutputContent": json.dumps({"status": "passed", "summary": "baseline complete", "outputs": {"score": 0.7}})}
    assert poll_step(ssm, command_id="cmd-123", instance_id="i-0123456789abcdef0") == {"terminal": True, "status": "Success", "passed": True, "summary": "baseline complete", "outputs": {"score": 0.7}, "error": "", "documentVersion": None}


@pytest.mark.parametrize("stdout", [
    '{"status": "passed", "summary": "cut off at the SSM ca',
    '{"status": "passed", "outputs": {}}\n---Output truncated---',
    '["not", "an", "object"]',
    "plain text from a crashed parser",
    "",
])
def test_malformed_or_truncated_output_is_a_failed_step(stdout):
    result = parse_invocation({"Status": "Success", "StandardOutputContent": stdout, "DocumentVersion": "3"})
    assert result["passed"] is False and result["summary"] == MALFORMED_OUTPUT
    assert result["error"].startswith(MALFORMED_OUTPUT) and result["documentVersion"] == "3"
    assert result["outputs"]["outputError"] == MALFORMED_OUTPUT and result["outputs"]["stdoutChars"] == len(stdout)


def test_malformed_failed_step_keeps_stderr_and_empty_failure_keeps_old_message():
    result = parse_invocation({"Status": "Failed", "StandardOutputContent": "{oops", "StandardErrorContent": "Traceback: boom"})
    assert result["passed"] is False and "Traceback: boom" in result["error"] and result["error"].startswith(MALFORMED_OUTPUT)
    result = parse_invocation({"Status": "TimedOut", "StandardErrorContent": "timed out"})
    assert result["passed"] is False and result["error"] == "timed out" and result["summary"] == "step failed"


def test_document_version_is_recorded():
    ok = json.dumps({"status": "passed", "summary": "done", "outputs": {}})
    assert parse_invocation({"Status": "Success", "StandardOutputContent": ok, "DocumentVersion": "12"})["documentVersion"] == "12"
    assert parse_invocation({"Status": "Success", "StandardOutputContent": ok, "DocumentVersion": ""})["documentVersion"] is None
    assert parse_invocation({"Status": "Success", "StandardOutputContent": ok})["documentVersion"] is None
