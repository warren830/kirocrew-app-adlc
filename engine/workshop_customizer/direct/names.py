"""Resource names of a pack in direct mode: derived from its namespace, never equal to the Workshop's."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: Every direct-mode resource carries this tag (``adlc:pack`` names the pack), so an inventory or a cleanup
#: finds exactly them.
MODE_TAG = {"adlc:mode": "direct"}
#: The Harness name starts with this, never with the agent name: the Workshop selects its Harness and runtime by
#: prefix (99-cleanup ``starts_with(harnessName,'<agent>_')``; 09 and 13 take the first runtime starting with
#: ``harness_<agent>_``), so ``<agent>_direct`` would be scored, or deleted, as the class's agent.
HARNESS_PREFIX = "direct_"


def direct_runtime(name: str, agent: str) -> bool:
    """True for the runtime of a pack's direct Harness (named ``harness_<harness name>``)."""
    return name == f"harness_{HARNESS_PREFIX}{agent}"


@dataclass(frozen=True)
class DirectNames:
    agent: str               # the namespace's agentName (letters and digits)
    harness: str             # Harness names allow no hyphens
    harness_role: str
    memory: str
    gateway: str
    gateway_role: str
    target: str              # the Gateway target, without hyphens like the Workshop's: tool names stay identical
    lambda_function: str
    lambda_role: str
    knowledge_base: str
    kb_prefix: str           # the documents' prefix in the bucket
    skills_prefix: str
    bucket: str

    @classmethod
    def of(cls, namespace: Mapping[str, Any], *, account: str, region: str) -> "DirectNames":
        agent = str(namespace["agentName"])
        if f"{HARNESS_PREFIX}{agent}" == f"{agent}_{agent}":  # the Workshop's Harness is <agent>_<agent>
            raise ValueError(f"agent name {agent!r} would give the direct Harness the Workshop's name")
        return cls(
            agent=agent,
            harness=f"{HARNESS_PREFIX}{agent}",
            harness_role=f"{agent}-direct-harness",
            memory=f"{agent}_direct_memory",
            gateway=f"{namespace['gatewayName']}-direct",
            gateway_role=f"{agent}-direct-gateway",
            target=str(namespace["toolTargetName"]).replace("-", ""),
            lambda_function=f"{namespace['lambdaFunctionName']}-direct",
            lambda_role=f"{namespace['lambdaFunctionName']}-direct",
            knowledge_base=f"{namespace['knowledgeBaseName']}-direct",
            kb_prefix=f"direct/{agent}/kb/",
            skills_prefix=f"direct/{agent}/skills/",
            bucket=f"adlc-direct-{account}-{region}",
        )

    def tags(self, pack_id: str) -> dict[str, str]:
        return {**MODE_TAG, "adlc:pack": pack_id}
