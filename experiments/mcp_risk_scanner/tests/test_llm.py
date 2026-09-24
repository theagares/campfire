from __future__ import annotations

import json
import unittest

import httpx

from experiments.mcp_risk_scanner.core import assess
from experiments.mcp_risk_scanner.llm import review_with_solar


class LlmTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_explicit_cloud_consent(self):
        with self.assertRaises(ValueError):
            await review_with_solar([], consent=False, api_key="fake")

    async def test_redacts_secret_and_suggestions_do_not_change_score(self):
        observed: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed.update(json.loads(request.content))
            answer = {"suspicions": [
                {"tool": "lookup", "field": "description", "reason": "Potential cross-server instruction", "severity": "warning"},
                {"tool": "invented", "field": "description", "reason": "not in input", "severity": "critical"},
            ]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

        tools = [{"name": "lookup", "description": "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"}]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            suggestions = await review_with_solar(tools, consent=True, api_key="fake", client=client)
        self.assertEqual(len(suggestions), 1)
        self.assertNotIn("ghp_", json.dumps(observed))
        report = assess("fixture", tools)
        before = report.risk_score
        report.llm_suggestions = suggestions
        self.assertEqual(report.risk_score, before)
        self.assertEqual(report.verdict, "review")


if __name__ == "__main__":
    unittest.main()
