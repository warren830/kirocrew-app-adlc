"""The ADLC console's platform side: workspaces, agents, knowledge bases, chat, evaluation, registry, governance.

Everything here is boto3 against AgentCore (``bedrock-agentcore-control`` / ``bedrock-agentcore``) and the services
around it, scoped to one *workspace* (an AWS account and region, reached through a local profile or an assumed
role). ``app/console/server.py`` exposes it as ``/api/console/...`` next to the workshop App's own backend; the
modules keep no state of their own beyond the console data dir (``store``).
"""
