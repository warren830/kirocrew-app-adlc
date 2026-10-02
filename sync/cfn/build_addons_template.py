#!/usr/bin/env python3
"""Build ``customizer-addons.json`` for safe release sync and Guided Workshop execution.

The stack owns both executable SSM documents. The App may invoke only the release applier or the
allow-listed 00→13 step runner on the one workshop instance; generic shell documents remain denied.
"""

from __future__ import annotations

import base64
import json
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
APPLIER = HERE.parent / "host" / "apply_release.py"
RUNTIME_PERMISSIONS = HERE.parent / "host" / "runtime_permissions.py"
APPLY_DOCUMENT = HERE.parent / "ssm" / "WorkshopCustomizerApplyRelease.json"
RUN_DOCUMENT = HERE.parent / "ssm" / "WorkshopCustomizerRunStep.json"
OUTPUT = HERE / "customizer-addons.json"

RELEASE_PREFIX = "customizer-releases/"
CONTRACT_PARAM = "/workshop-customizer/target-contract"
APPLY_DOC_NAME = "WorkshopCustomizerApplyRelease"
RUN_DOC_NAME = "WorkshopCustomizerRunStep"
INSTALL_DOC_NAME = "WorkshopCustomizerInstallApplier"
SSM_DOCUMENT_CONTENT_LIMIT = 64 * 1024


def document_sha256(document: dict) -> str:
    """Canonical sha256 of an SSM document's content (sorted keys, compact separators).

    The same function lives in ``engine/workshop_customizer/sync.py`` (``run_document_sha256``); the
    preflight compares this stack output with the local ``sync/ssm/WorkshopCustomizerRunStep.json`` so a
    stale add-ons stack (an older RunStep parser) can never run Guided steps.
    """
    import hashlib

    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def sub(template: str) -> dict:
    return {"Fn::Sub": template}


def install_document(applier_b64: str) -> dict:
    """Custom SSM Command document that installs the applier and the contract file."""
    cli_version = json.loads((HERE.parents[1] / "template-lock.json").read_text())["runtime"]["agentcoreCli"]
    runtime_permissions_b64 = base64.b64encode(RUNTIME_PERMISSIONS.read_bytes()).decode("ascii")
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "install -d -m 0755 /opt/workshop-customizer /etc/workshop-customizer",
        "exec 9>/opt/workshop-customizer/install.lock",
        "flock -x 9",
        "if ! /usr/bin/python3.12 -m pip --version >/dev/null 2>&1; then dnf install -y python3.12 python3.12-pip; fi",
        "install -d -m 0755 /opt/workshop-customizer/bin",
        "ln -sfn /usr/bin/python3.12 /opt/workshop-customizer/bin/python3",
        'CLI_VERSION="$(/usr/local/bin/npx --no-install agentcore --version 2>/dev/null || true)"',
        f'if [ "$CLI_VERSION" != "{cli_version}" ]; then',
        f"  /usr/local/bin/npm install -g @aws/agentcore@{cli_version}",
        "fi",
        "cat > /opt/workshop-customizer/apply_release.b64 <<'B64EOF'",
        *textwrap.wrap(applier_b64, 76),
        "B64EOF",
        "base64 -d /opt/workshop-customizer/apply_release.b64 > /opt/workshop-customizer/apply_release.py.tmp",
        "rm -f /opt/workshop-customizer/apply_release.b64",
        "python3 -m py_compile /opt/workshop-customizer/apply_release.py.tmp",
        "mv -f /opt/workshop-customizer/apply_release.py.tmp /opt/workshop-customizer/apply_release.py",
        "chmod 0755 /opt/workshop-customizer/apply_release.py",
        "cat > /opt/workshop-customizer/runtime_permissions.b64 <<'RUNTIME_PERMISSIONS_EOF'",
        *textwrap.wrap(runtime_permissions_b64, 76),
        "RUNTIME_PERMISSIONS_EOF",
        "base64 -d /opt/workshop-customizer/runtime_permissions.b64 > /opt/workshop-customizer/runtime_permissions.py",
        "rm -f /opt/workshop-customizer/runtime_permissions.b64",
        "chmod 0755 /opt/workshop-customizer/runtime_permissions.py",
        "aws ssm get-parameter --region '{{ Region }}' --name '{{ ContractParameterName }}' --query Parameter.Value --output text > /etc/workshop-customizer/target.json.tmp",
        "python3 -c 'import json; json.load(open(\"/etc/workshop-customizer/target.json.tmp\"))'",
        "mv -f /etc/workshop-customizer/target.json.tmp /etc/workshop-customizer/target.json",
        "chmod 0644 /etc/workshop-customizer/target.json",
        "id '{{ TargetUser }}' >/dev/null 2>&1 || useradd -m '{{ TargetUser }}'",
        "runuser -u '{{ TargetUser }}' -- /usr/bin/python3.12 -m pip install --user boto3 botocore retrying uv",
        "install -d -m 0755 -o '{{ TargetUser }}' -g '{{ TargetGroup }}' '{{ TargetRoot }}' '{{ TargetRoot }}/releases'",
        "echo 'workshop-customizer applier installed'",
    ]
    return {
        "schemaVersion": "2.2",
        "description": "Workshop Customizer: install the release applier and target contract on the workshop EC2.",
        "parameters": {
            "TargetRoot": {"type": "String", "default": "/home/ssm-user/workshop", "allowedPattern": "^/[A-Za-z0-9._/-]+$"},
            "TargetUser": {"type": "String", "default": "ssm-user", "allowedPattern": "^[a-z_][a-z0-9_-]{0,31}$"},
            "TargetGroup": {"type": "String", "default": "ssm-user", "allowedPattern": "^[a-z_][a-z0-9_-]{0,31}$"},
            "ContractParameterName": {"type": "String", "default": CONTRACT_PARAM, "allowedPattern": "^/[A-Za-z0-9._/-]+$"},
            "Region": {"type": "String", "default": "us-west-2", "allowedPattern": "^[a-z]{2}(-[a-z]+)+-[0-9]$"},
        },
        "mainSteps": [{"action": "aws:runShellScript", "name": "installApplier", "precondition": {"StringEquals": ["platformType", "Linux"]}, "inputs": {"timeoutSeconds": "600", "runCommand": lines}}],
    }


def build() -> dict:
    applier_b64 = base64.b64encode(APPLIER.read_bytes()).decode("ascii")
    apply_document = json.loads(APPLY_DOCUMENT.read_text(encoding="utf-8"))
    run_document = json.loads(RUN_DOCUMENT.read_text(encoding="utf-8"))
    install_doc = install_document(applier_b64)
    install_size = len(json.dumps(install_doc))
    if install_size > SSM_DOCUMENT_CONTENT_LIMIT:
        raise SystemExit(f"install document is {install_size} bytes; SSM document content limit is {SSM_DOCUMENT_CONTENT_LIMIT}")
    for name, document in ((APPLY_DOC_NAME, apply_document), (RUN_DOC_NAME, run_document)):
        size = len(json.dumps(document))
        if size > SSM_DOCUMENT_CONTENT_LIMIT:
            raise SystemExit(f"{name} is {size} bytes; SSM document content limit is {SSM_DOCUMENT_CONTENT_LIMIT}")

    bucket = {"Fn::ImportValue": sub("${WorkshopStackName}-SkillsBucketName")}
    contract_template = json.dumps(
        {
            "schema": "workshop-customizer/target-contract/1",
            "targetRoot": "${TargetRoot}",
            "user": "${TargetUser}",
            "group": "${TargetGroup}",
            "templateCommit": "${TemplateCommit}",
            "packSchemaVersion": 1,
            "allowedReleasePrefixes": ["s3://${WorkshopBucket}/" + RELEASE_PREFIX],
            "documentName": APPLY_DOC_NAME,
            "runDocumentName": RUN_DOC_NAME,
            "instanceId": "${WorkshopInstanceId}",
            "workshopStack": "${WorkshopStackName}",
            "addonsStack": "${AWS::StackName}",
            "region": "${AWS::Region}",
            "comparisonModel": {
                "id": "${ComparisonModelId}",
                "inputPricePerMillionUsd": "${ComparisonInputPrice}",
                "outputPricePerMillionUsd": "${ComparisonOutputPrice}",
            },
        },
        separators=(",", ":"),
    )
    contract_value = {"Fn::Sub": [contract_template, {"WorkshopBucket": bucket}]}

    def bucket_arn(suffix: str = "") -> dict:
        return {"Fn::Sub": ["arn:${AWS::Partition}:s3:::${WorkshopBucket}" + suffix, {"WorkshopBucket": bucket}]}

    apply_doc_arn = sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:document/" + APPLY_DOC_NAME)
    run_doc_arn = sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:document/" + RUN_DOC_NAME)
    allowed_document_arns = [apply_doc_arn, run_doc_arn]

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Workshop Customizer add-ons: target contract, fixed SSM documents, applier install and least-privilege sync/run policy.",
        "Metadata": {"WorkshopCustomizer": {"applierSha256": _sha256(APPLIER), "installDocumentBytes": install_size, "runDocumentBytes": len(json.dumps(run_document)), "runDocumentSha256": document_sha256(run_document)}},
        "Parameters": {
            "WorkshopStackName": {"Type": "String", "Default": "workshop-infra", "Description": "Name of the upstream workshop stack (exports SkillsBucketName)."},
            "WorkshopInstanceId": {"Type": "AWS::EC2::Instance::Id", "Description": "InstanceId output of the workshop stack (the students' EC2)."},
            "TemplateCommit": {"Type": "String", "AllowedPattern": "^[0-9a-f]{40}$", "Description": "Pinned upstream commit every synced release must declare."},
            "TargetRoot": {"Type": "String", "Default": "/home/ssm-user/workshop", "AllowedPattern": "^/[A-Za-z0-9._/-]+$"},
            "TargetUser": {"Type": "String", "Default": "ssm-user", "AllowedPattern": "^[a-z_][a-z0-9_-]{0,31}$"},
            "TargetGroup": {"Type": "String", "Default": "ssm-user", "AllowedPattern": "^[a-z_][a-z0-9_-]{0,31}$"},
            "AgentName": {"Type": "String", "Default": "hrassistant", "AllowedPattern": "^[a-z][a-z0-9_-]{2,60}$"},
            "ToolTargetName": {"Type": "String", "Default": "hr-tools", "AllowedPattern": "^[a-z][a-z0-9-]{2,60}$"},
            "ToolFunctionName": {"Type": "String", "Default": "hr-tools-handler", "AllowedPattern": "^[a-z][a-z0-9-]{2,60}$"},
            "KnowledgeBaseName": {"Type": "String", "Default": "hr-knowledge-base", "AllowedPattern": "^[a-z][a-z0-9-]{2,60}$"},
            "SsmParameterPrefix": {"Type": "String", "Default": "/app/hr", "AllowedPattern": "^/app/[a-z][a-z0-9-]{1,60}$"},
            "ComparisonModelId": {"Type": "String", "Default": "us.amazon.nova-pro-v1:0",
                                  "AllowedPattern": "^[a-zA-Z0-9][a-zA-Z0-9._:/-]{1,200}$"},
            "ComparisonInputPrice": {"Type": "Number", "Default": 0.8, "MinValue": 0},
            "ComparisonOutputPrice": {"Type": "Number", "Default": 3.2, "MinValue": 0},
        },
        "Resources": {
            "TargetContractParameter": {"Type": "AWS::SSM::Parameter", "Properties": {"Name": CONTRACT_PARAM, "Type": "String", "Description": "Workshop Customizer target contract — trust anchor read by the App before any SendCommand.", "Value": contract_value}},
            "InstallApplierDocument": {"Type": "AWS::SSM::Document", "Properties": {"Name": INSTALL_DOC_NAME, "DocumentType": "Command", "DocumentFormat": "JSON", "UpdateMethod": "NewVersion", "Content": install_doc}},
            "ApplyReleaseDocument": {"Type": "AWS::SSM::Document", "Properties": {"Name": APPLY_DOC_NAME, "DocumentType": "Command", "DocumentFormat": "JSON", "UpdateMethod": "NewVersion", "Content": apply_document}},
            "RunStepDocument": {"Type": "AWS::SSM::Document", "Properties": {"Name": RUN_DOC_NAME, "DocumentType": "Command", "DocumentFormat": "JSON", "UpdateMethod": "NewVersion", "Content": run_document}},
            "InstallApplierAssociation": {
                "Type": "AWS::SSM::Association",
                "DependsOn": ["TargetContractParameter"],
                "Properties": {
                    "AssociationName": sub("${AWS::StackName}-install-applier"),
                    "Name": {"Ref": "InstallApplierDocument"},
                    "Targets": [{"Key": "InstanceIds", "Values": [{"Ref": "WorkshopInstanceId"}]}],
                    "Parameters": {"TargetRoot": [{"Ref": "TargetRoot"}], "TargetUser": [{"Ref": "TargetUser"}], "TargetGroup": [{"Ref": "TargetGroup"}], "ContractParameterName": [CONTRACT_PARAM], "Region": [{"Ref": "AWS::Region"}]},
                    "MaxErrors": "0",
                },
            },
            "SyncPolicy": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "ManagedPolicyName": sub("${AWS::StackName}-sync"),
                    "Description": "Least privilege for release sync and Guided Workshop execution on one instance through two fixed documents.",
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {"Sid": "Preflight", "Effect": "Allow", "Action": ["sts:GetCallerIdentity", "ssm:DescribeInstanceInformation"], "Resource": "*"},
                            {"Sid": "ReadStacks", "Effect": "Allow", "Action": ["cloudformation:DescribeStacks"], "Resource": [sub("arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/${WorkshopStackName}/*"), sub("arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/${AWS::StackName}/*")]},
                            {"Sid": "ReadContract", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters"], "Resource": sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter" + CONTRACT_PARAM)},
                            {"Sid": "GuardReadPackParameters", "Effect": "Allow", "Action": ["ssm:GetParameter"], "Resource": sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/app/*")},
                            {"Sid": "GuardListAgentRuntimes", "Effect": "Allow", "Action": ["bedrock-agentcore:ListAgentRuntimes"], "Resource": "*"},
                            {"Sid": "ListReleasePrefixOnly", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": bucket_arn(), "Condition": {"StringLike": {"s3:prefix": [RELEASE_PREFIX + "*"]}}},
                            {"Sid": "ReleaseObjects", "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"], "Resource": bucket_arn("/" + RELEASE_PREFIX + "*")},
                            {"Sid": "InvokeOnlyFixedDocumentsOnWorkshopInstance", "Effect": "Allow", "Action": ["ssm:SendCommand"], "Resource": [sub("arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:instance/${WorkshopInstanceId}"), *allowed_document_arns]},
                            {"Sid": "ReadCommandResults", "Effect": "Allow", "Action": ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ssm:ListCommands"], "Resource": "*"},
                            {"Sid": "DenyEveryOtherDocument", "Effect": "Deny", "Action": ["ssm:SendCommand", "ssm:StartSession", "ssm:CreateAssociation", "ssm:UpdateAssociation", "ssm:StartAutomationExecution"], "Resource": "arn:*:ssm:*:*:document/*", "Condition": {"ArnNotEquals": {"ssm:DocumentArn": allowed_document_arns}}},
                            {"Sid": "DenyWritesOutsideReleasePrefix", "Effect": "Deny", "Action": ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutBucketPolicy", "s3:PutBucketAcl", "s3:PutObjectAcl"], "NotResource": bucket_arn("/" + RELEASE_PREFIX + "*")},
                            {"Sid": "DenyDestructiveAndIam", "Effect": "Deny", "Action": ["iam:*", "ec2:TerminateInstances", "ec2:StopInstances", "ec2:ModifyInstanceAttribute", "cloudformation:DeleteStack", "cloudformation:UpdateStack", "bedrock-agentcore:Delete*", "lambda:Delete*", "s3:DeleteBucket"], "Resource": "*"},
                        ],
                    },
                },
            },
        },
        "Outputs": {
            "ApplyDocumentName": {"Value": {"Ref": "ApplyReleaseDocument"}},
            "RunStepDocumentName": {"Value": {"Ref": "RunStepDocument"}},
            "RunStepDocumentSha256": {"Value": document_sha256(run_document), "Description": "Canonical sha256 of the RunStep document content; preflight compares it with the local sync/ssm/WorkshopCustomizerRunStep.json."},
            "InstallDocumentName": {"Value": {"Ref": "InstallApplierDocument"}},
            "TargetContractParameter": {"Value": CONTRACT_PARAM},
            "ReleasePrefix": {"Value": {"Fn::Sub": ["s3://${WorkshopBucket}/" + RELEASE_PREFIX, {"WorkshopBucket": bucket}]}},
            "SyncPolicyArn": {"Value": {"Ref": "SyncPolicy"}},
            "WorkshopInstanceId": {"Value": {"Ref": "WorkshopInstanceId"}},
        },
    }
    # These permissions belong to the Workshop EC2, independently of the SA's restrictive SyncPolicy.
    # The upstream instance role does not include AgentCore, S3 Vectors, tool Lambda updates, or
    # the knowledge-base helper's IAM names. Bind the additions to this region and pack namespace.
    template["Resources"]["WorkshopExecutionPolicy"] = {
        "Type": "AWS::IAM::Policy",
        "Properties": {
            "PolicyName": sub("${AWS::StackName}-execution"),
            "Roles": [sub("workshop-ec2-role-${AWS::Region}")],
            "PolicyDocument": {"Version": "2012-10-17", "Statement": [
                {"Sid": "AgentCoreWorkshop", "Effect": "Allow",
                 "Action": ["bedrock-agentcore:Create*", "bedrock-agentcore:Get*", "bedrock-agentcore:List*",
                            "bedrock-agentcore:Update*", "bedrock-agentcore:Invoke*", "bedrock-agentcore:Evaluate",
                            "bedrock-agentcore:TagResource", "bedrock-agentcore:UntagResource"],
                 "Resource": "*", "Condition": {"StringEquals": {"aws:RequestedRegion": {"Ref": "AWS::Region"}}}},
                {"Sid": "ScenarioLambdas", "Effect": "Allow",
                 "Action": ["lambda:CreateFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration",
                            "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
                            "lambda:AddPermission", "lambda:GetPolicy", "lambda:InvokeFunction",
                            "lambda:ListTags", "lambda:TagResource"],
                 "Resource": [sub("arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:${ToolFunctionName}"),
                              sub("arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:${AgentName}-*")]},
                {"Sid": "ScenarioParameters", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:PutParameter"],
                 "Resource": sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${SsmParameterPrefix}/*")},
                {"Sid": "ScenarioVectors", "Effect": "Allow",
                 "Action": ["s3vectors:CreateVectorBucket", "s3vectors:GetVectorBucket", "s3vectors:CreateIndex",
                            "s3vectors:GetIndex", "s3vectors:ListIndexes", "s3vectors:PutVectorBucketPolicy",
                            "s3vectors:GetVectorBucketPolicy", "s3vectors:DeleteIndex", "s3vectors:DeleteVectorBucket"],
                 "Resource": [sub("arn:${AWS::Partition}:s3vectors:${AWS::Region}:${AWS::AccountId}:bucket/${KnowledgeBaseName}-*"),
                              sub("arn:${AWS::Partition}:s3vectors:${AWS::Region}:${AWS::AccountId}:vector-bucket/${KnowledgeBaseName}-*")]},
                {"Sid": "WorkshopRoleManagement", "Effect": "Allow",
                 "Action": ["iam:CreateRole", "iam:GetRole", "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy",
                            "iam:GetRolePolicy", "iam:AttachRolePolicy", "iam:ListRolePolicies",
                            "iam:ListAttachedRolePolicies", "iam:PassRole", "iam:TagRole",
                            "iam:DeleteRolePolicy", "iam:DetachRolePolicy", "iam:DeleteRole"],
                 "Resource": [sub("arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${AgentName}*"),
                              sub("arn:${AWS::Partition}:iam::${AWS::AccountId}:role/AgentCore-${AgentName}*"),
                              sub("arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${ToolTargetName}*"),
                              sub("arn:${AWS::Partition}:iam::${AWS::AccountId}:role/AmazonBedrockExecutionRoleForKnowledgeBase_*")]},
                {"Sid": "KnowledgeBasePolicies", "Effect": "Allow",
                 "Action": ["iam:CreatePolicy", "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions",
                            "iam:CreatePolicyVersion", "iam:DeletePolicy", "iam:DeletePolicyVersion"],
                 "Resource": [sub("arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/AmazonBedrock*PolicyForKnowledgeBase_*")]},
                {"Sid": "WorkshopLogs", "Effect": "Allow",
                 "Action": ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:StartQuery", "logs:DescribeLogStreams",
                            "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutRetentionPolicy"],
                 "Resource": [sub("arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:aws/spans:*"),
                              sub("arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/bedrock-agentcore/*"),
                              sub("arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/application-signals/data:*"),
                              sub("arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${AgentName}-*:*")]},
                {"Sid": "EvaluatorLogReplacement", "Effect": "Allow", "Action": ["logs:DeleteLogGroup"],
                 "Resource": sub("arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${AgentName}-eval-*:*")},
                {"Sid": "TransactionSearch", "Effect": "Allow",
                 "Action": ["xray:GetTraceSegmentDestination", "xray:UpdateTraceSegmentDestination",
                            "xray:GetIndexingRules", "xray:UpdateIndexingRule", "logs:DescribeLogGroups",
                            "logs:DescribeResourcePolicies", "logs:PutResourcePolicy", "logs:GetQueryResults"],
                 "Resource": "*", "Condition": {"StringEquals": {"aws:RequestedRegion": {"Ref": "AWS::Region"}}}},
            ]},
        },
    }
    template["Parameters"]["GatewayName"] = {
        "Type": "String", "Default": "hrgateway", "AllowedPattern": "[a-z][a-z0-9-]{2,99}",
    }
    template["Resources"]["WorkshopExecutionPolicy"]["Properties"]["PolicyDocument"]["Statement"].extend([
        {"Sid": "DeleteScenarioGateway", "Effect": "Allow",
         "Action": ["bedrock-agentcore:DeleteGateway", "bedrock-agentcore:DeleteGatewayTarget"],
         "Resource": sub("arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:gateway/${GatewayName}-*")},
        {"Sid": "DeleteScenarioAgent", "Effect": "Allow",
         "Action": ["bedrock-agentcore:DeleteHarness", "bedrock-agentcore:DeleteAgentRuntime",
                    "bedrock-agentcore:DeleteMemory", "bedrock-agentcore:DeleteEvaluator"],
         "Resource": [sub("arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:harness/${AgentName}_*"),
                      sub("arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:runtime/harness_${AgentName}_*"),
                      sub("arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:memory/${AgentName}_*"),
                      sub("arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:evaluator/${AgentName}_*")]},
        {"Sid": "DeleteScenarioParameters", "Effect": "Allow", "Action": ["ssm:DeleteParameter"],
         "Resource": sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${SsmParameterPrefix}/*")},
        # 99-cleanup.sh --scenario-only lists this scenario's parameters by path and deletes its tools
        # Lambda (never a function workshop-infra owns; the script checks and fails closed) with the log
        # group, so a changed pack can be re-synced into the same environment. Scoped to the pack
        # namespace like the deletes above; these are the policy's only Lambda / log-group deletes
        # besides the evaluator log replacement.
        {"Sid": "ScenarioOnlyCleanupListParameters", "Effect": "Allow", "Action": ["ssm:GetParametersByPath"],
         "Resource": [sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${SsmParameterPrefix}"),
                      sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${SsmParameterPrefix}/*")]},
        {"Sid": "ScenarioOnlyCleanupLambda", "Effect": "Allow", "Action": ["lambda:DeleteFunction"],
         "Resource": sub("arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:${ToolFunctionName}")},
        {"Sid": "ScenarioOnlyCleanupLambdaLogs", "Effect": "Allow", "Action": ["logs:DeleteLogGroup"],
         "Resource": [sub("arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${ToolFunctionName}"),
                      sub("arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${ToolFunctionName}:*")]},
        {"Sid": "DeleteScenarioWorkloadIdentities", "Effect": "Allow",
         "Action": ["bedrock-agentcore:DeleteWorkloadIdentity"],
         "Resource": [
             sub("arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:workload-identity-directory/default"),
             sub("arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:workload-identity-directory/default/workload-identity/${GatewayName}-*"),
             sub("arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:workload-identity-directory/default/workload-identity/${AgentName}_*"),
         ]},
    ])
    vpc = {"Fn::ImportValue": sub("${WorkshopStackName}-VPCId")}
    harness_sg = {"Fn::ImportValue": sub("${WorkshopStackName}-SecurityGroupId")}
    subnets = {"Fn::Split": [",", {"Fn::ImportValue": sub("${WorkshopStackName}-PrivateSubnets")}]}
    template["Resources"].update({
        "SkillsFilesRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{
                    "Effect": "Allow", "Principal": {"Service": "elasticfilesystem.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
                                  "ArnLike": {"aws:SourceArn": sub("arn:${AWS::Partition}:s3files:${AWS::Region}:${AWS::AccountId}:file-system/*")}},
                }]},
                "Policies": [{"PolicyName": "WorkshopSkillsFiles", "PolicyDocument": {
                    "Version": "2012-10-17", "Statement": [
                        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:ListBucketVersions"], "Resource": bucket_arn()},
                        {"Effect": "Allow", "Action": ["s3:AbortMultipartUpload", "s3:DeleteObject*", "s3:GetObject*", "s3:List*", "s3:PutObject*"],
                         "Resource": bucket_arn("/*")},
                        {"Effect": "Allow", "Action": ["events:DeleteRule", "events:DisableRule", "events:EnableRule",
                                                        "events:PutRule", "events:PutTargets", "events:RemoveTargets"],
                         "Resource": sub("arn:${AWS::Partition}:events:${AWS::Region}:${AWS::AccountId}:rule/DO-NOT-DELETE-S3-Files*"),
                         "Condition": {"StringEquals": {"events:ManagedBy": "elasticfilesystem.amazonaws.com"}}},
                        {"Effect": "Allow", "Action": ["events:DescribeRule", "events:ListRuleNamesByTarget", "events:ListRules", "events:ListTargetsByRule"],
                         "Resource": sub("arn:${AWS::Partition}:events:${AWS::Region}:${AWS::AccountId}:rule/*")},
                    ],
                }}],
            },
        },
        "SkillsFileSystem": {"Type": "AWS::S3Files::FileSystem", "Properties": {
            "Bucket": bucket_arn(), "RoleArn": {"Fn::GetAtt": ["SkillsFilesRole", "Arn"]},
            "Tags": [{"Key": "Name", "Value": sub("${AWS::StackName}-skills")}],
        }},
        "SkillsFilesAccessPoint": {"Type": "AWS::S3Files::AccessPoint", "Properties": {
            "FileSystemId": {"Ref": "SkillsFileSystem"}, "PosixUser": {"Uid": "1000", "Gid": "1000"},
            "RootDirectory": {"Path": "/"},
        }},
        "SkillsFilesSecurityGroup": {"Type": "AWS::EC2::SecurityGroup", "Properties": {
            "GroupDescription": "NFS only from the Workshop harness security group", "VpcId": vpc,
            "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 2049, "ToPort": 2049, "SourceSecurityGroupId": harness_sg}],
        }},
        "HarnessSkillsFilesEgress": {"Type": "AWS::EC2::SecurityGroupEgress", "Properties": {
            "GroupId": harness_sg, "IpProtocol": "tcp", "FromPort": 2049, "ToPort": 2049,
            "DestinationSecurityGroupId": {"Ref": "SkillsFilesSecurityGroup"},
        }},
    })
    for index in range(2):
        template["Resources"][f"SkillsFilesMountTarget{index + 1}"] = {
            "Type": "AWS::S3Files::MountTarget",
            "Properties": {"FileSystemId": {"Ref": "SkillsFileSystem"},
                           "SubnetId": {"Fn::Select": [index, subnets]},
                           "SecurityGroups": [{"Ref": "SkillsFilesSecurityGroup"}], "IpAddressType": "IPV4_ONLY"},
        }
    template["Outputs"]["SkillsFilesAccessPointArn"] = {"Value": {"Fn::GetAtt": ["SkillsFilesAccessPoint", "AccessPointArn"]}}
    template["Outputs"]["SkillsFileSystemArn"] = {"Value": {"Fn::GetAtt": ["SkillsFileSystem", "FileSystemArn"]}}
    template["Resources"]["WorkshopExecutionPolicy"]["Properties"]["PolicyDocument"]["Statement"].append({
        "Sid": "ReadSkillsFileSystem", "Effect": "Allow",
        "Action": ["s3files:GetFileSystem", "s3files:GetAccessPoint", "s3files:ListMountTargets", "s3files:ListAccessPoints"],
        "Resource": [{"Fn::GetAtt": ["SkillsFileSystem", "FileSystemArn"]},
                     {"Fn::GetAtt": ["SkillsFilesAccessPoint", "AccessPointArn"]}],
    })
    return template


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    template = build()
    OUTPUT.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
