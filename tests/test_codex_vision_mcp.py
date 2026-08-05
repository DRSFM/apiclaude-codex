from __future__ import annotations

import unittest

import codex_vision_mcp
from codex_vision_proxy import VisionInspection


class CodexVisionMcpTests(unittest.TestCase):
    def test_initialize_exposes_model_guidance_and_visual_call_status_rule(self) -> None:
        server = codex_vision_mcp.VisionMcpServer(
            inspect=lambda *_args, **_kwargs: self.fail("tool was called")
        )

        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )

        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("mcp__apicodex_vision__inspect_images", result["instructions"])
        self.assertIn("Do not call", result["instructions"])
        self.assertIn("视觉辅助：本轮已调用 Gemini", result["instructions"])
        self.assertIn("视觉辅助：本轮未调用 Gemini", result["instructions"])

    def test_tool_result_distinguishes_gemini_call_from_cache_hit(self) -> None:
        calls: list[tuple[list[str], str, bool]] = []

        def inspect(image_ids: list[str], focus: str, refresh: bool) -> VisionInspection:
            calls.append((image_ids, focus, refresh))
            return VisionInspection(
                image_ids=tuple(image_ids),
                description="The first drawing has warmer lighting.",
                gemini_invoked=False,
                cache_hit=True,
            )

        server = codex_vision_mcp.VisionMcpServer(inspect=inspect)
        listed = server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        tool = listed["result"]["tools"][0]
        self.assertEqual(tool["name"], "inspect_images")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertFalse(tool["annotations"]["openWorldHint"])

        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "inspect_images",
                    "arguments": {
                        "image_ids": ["sha256:first"],
                        "focus": "Compare the drawings.",
                    },
                },
            }
        )

        self.assertEqual(calls, [(["sha256:first"], "Compare the drawings.", False)])
        text = response["result"]["content"][0]["text"]
        self.assertIn("gemini_invoked=false", text)
        self.assertIn("cache_hit=true", text)
        self.assertIn("warmer lighting", text)


if __name__ == "__main__":
    unittest.main()
