"""Deploy the checked-in add-ons template, using S3 for CloudFormation's large-template path.

Explicitly selects and verifies an account; updates only the named Workshop add-ons stack.
The installation association is rerun after an update so host dependencies and the applier converge.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--workshop-stack", default="workshop-infra")
    parser.add_argument("--addons-stack", default="workshop-customizer-addons")
    # Required: the add-ons bind the Workshop EC2's execution permissions to ONE pack namespace, so a
    # silent default would rebind them to another pack (preflight check stack.addons.namespace).
    parser.add_argument("--scenario", type=Path, required=True,
                        help="the scenario.yaml of the pack this environment runs (its namespace is bound to the add-ons)")
    parser.add_argument("--comparison-model")
    parser.add_argument("--comparison-price-in")
    parser.add_argument("--comparison-price-out")
    args = parser.parse_args()
    comparison = [args.comparison_model, args.comparison_price_in, args.comparison_price_out]
    if any(value is not None for value in comparison) and not all(value is not None for value in comparison):
        parser.error("comparison model and both verified prices must be supplied together")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity()["Account"] != args.account:
        raise RuntimeError("AWS caller account differs from the requested account")
    cfn, ssm = session.client("cloudformation"), session.client("ssm")
    stack = cfn.describe_stacks(StackName=args.workshop_stack)["Stacks"][0]
    if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        raise RuntimeError("Workshop infrastructure is not ready")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
    body = (REPO / "sync/cfn/customizer-addons.json").read_bytes()
    key = "customizer-releases/infrastructure/" + hashlib.sha256(body).hexdigest()[:16] + "-addons.json"
    session.client("s3").put_object(Bucket=outputs["SkillsBucketName"], Key=key, Body=body,
                                     ContentType="application/json")
    parameters = [
        {"ParameterKey": "WorkshopStackName", "ParameterValue": args.workshop_stack},
        {"ParameterKey": "WorkshopInstanceId", "ParameterValue": outputs["InstanceId"]},
        {"ParameterKey": "TemplateCommit", "ParameterValue": json.loads((REPO / "template-lock.json").read_text())["template"]["commit"]},
    ]
    namespace = yaml.safe_load(args.scenario.read_text())["namespace"]
    for parameter, field in (("AgentName", "agentName"), ("ToolTargetName", "toolTargetName"),
                             ("GatewayName", "gatewayName"),
                             ("ToolFunctionName", "lambdaFunctionName"), ("KnowledgeBaseName", "knowledgeBaseName"),
                             ("SsmParameterPrefix", "ssmParameterPrefix")):
        parameters.append({"ParameterKey": parameter, "ParameterValue": namespace[field]})
    comparison_parameters = [
        ("ComparisonModelId", args.comparison_model),
        ("ComparisonInputPrice", args.comparison_price_in),
        ("ComparisonOutputPrice", args.comparison_price_out),
    ]
    if args.comparison_model:
        parameters.extend({"ParameterKey": key, "ParameterValue": value} for key, value in comparison_parameters)
    request = {
        "StackName": args.addons_stack,
        "TemplateURL": f"https://{outputs['SkillsBucketName']}.s3.{args.region}.amazonaws.com/{key}",
        "Parameters": parameters, "Capabilities": ["CAPABILITY_NAMED_IAM"],
    }
    try:
        existing = cfn.describe_stacks(StackName=args.addons_stack)["Stacks"][0]
    except ClientError as exc:
        if "does not exist" not in exc.response["Error"]["Message"]:
            raise
        cfn.create_stack(**request)
    else:
        if not args.comparison_model:
            previous_keys = {item["ParameterKey"] for item in existing.get("Parameters", [])}
            parameters.extend({"ParameterKey": key, "UsePreviousValue": True}
                              for key, _value in comparison_parameters if key in previous_keys)
        try:
            cfn.update_stack(**request)
        except ClientError as exc:
            if "No updates are to be performed" not in exc.response["Error"]["Message"]:
                raise
    while True:
        current = cfn.describe_stacks(StackName=args.addons_stack)["Stacks"][0]
        status = current["StackStatus"]
        print(status, flush=True)
        if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            break
        if not status.endswith("_IN_PROGRESS"):
            events = cfn.describe_stack_events(StackName=args.addons_stack)["StackEvents"]
            reasons = [{"resource": event["LogicalResourceId"], "reason": event.get("ResourceStatusReason")}
                       for event in events if event["ResourceStatus"].endswith("_FAILED")]
            raise RuntimeError(json.dumps(reasons[:5]))
        time.sleep(10)
    resources = cfn.list_stack_resources(StackName=args.addons_stack)["StackResourceSummaries"]
    association = next(item["PhysicalResourceId"] for item in resources
                       if item["LogicalResourceId"] == "InstallApplierAssociation")
    prior = {item["ExecutionId"] for item in ssm.describe_association_executions(
        AssociationId=association)["AssociationExecutions"]}
    ssm.start_associations_once(AssociationIds=[association])
    deadline = time.monotonic() + 660
    while time.monotonic() < deadline:
        executions = ssm.describe_association_executions(AssociationId=association)["AssociationExecutions"]
        fresh = [item for item in executions if item["ExecutionId"] not in prior]
        if fresh:
            execution = max(fresh, key=lambda item: item["CreatedTime"])
            if execution["Status"] == "Success":
                print("Host installation and target contract synchronized.", flush=True)
                break
            if execution["Status"] in ("Failed", "TimedOut", "Cancelled"):
                targets = ssm.describe_association_execution_targets(
                    AssociationId=association, ExecutionId=execution["ExecutionId"])["AssociationExecutionTargets"]
                raise RuntimeError("Host installation failed: " + json.dumps(targets, default=str))
        time.sleep(5)
    else:
        raise RuntimeError("Host installation did not complete; do not start the Guided Run yet")
    print(json.dumps({"instanceId": outputs["InstanceId"], "associationId": association}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except ClientError as exc:
        print(exc.response["Error"]["Code"] + ": " + exc.response["Error"]["Message"][:1000], file=sys.stderr)
        raise SystemExit(1)
