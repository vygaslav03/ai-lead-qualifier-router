import json

from app.llm import LLMClient, LLMError


class FakeLLM(LLMClient):
    """Returns scripted responses in order (dicts are JSON-encoded, exceptions are raised).
    When the script runs out it raises LLMError, like a provider outage."""

    provider = "fake"

    def __init__(self, responses=()):
        super().__init__("fake-model", max_retries=0)
        self.responses = [json.dumps(r) if isinstance(r, dict) else r for r in responses]
        self.prompts: list[str] = []

    async def _complete(self, system, user, *, json_mode, schema):
        self.prompts.append(user)
        if not self.responses:
            raise LLMError("fake LLM: no more scripted responses")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
