import json

from orchestra.ir.artifacts import ArtifactEnvelope
from orchestra.ir.contracts import AgentContract


def render_contract(
    contract: AgentContract, inputs: dict[str, ArtifactEnvelope]
) -> list[dict[str, str]]:
    payload = {
        slot: {
            "artifact_type": artifact.artifact_type,
            "payload": artifact.payload,
        }
        for slot, artifact in sorted(inputs.items())
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    user = contract.user_prompt_template.replace("{artifacts_json}", serialized)
    return [
        {"role": "system", "content": contract.system_prompt_template},
        {"role": "user", "content": user},
    ]
