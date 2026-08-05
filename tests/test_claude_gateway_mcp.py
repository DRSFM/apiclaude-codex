from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import claude_gateway_mcp


class ClaudeGatewayMcpTests(unittest.TestCase):
    def test_tools_list_exposes_search_and_weather(self) -> None:
        response = claude_gateway_mcp._handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            }
        )

        self.assertIsNotNone(response)
        names = {
            tool["name"]
            for tool in response["result"]["tools"]  # type: ignore[index]
        }
        self.assertEqual(names, {"web_search", "get_weather"})

    def test_web_search_parses_bing_rss_without_html_noise(self) -> None:
        rss = """<?xml version="1.0" encoding="utf-8"?>
<rss><channel>
  <item>
    <title>Weather &amp; forecast</title>
    <link>https://example.com/weather</link>
    <description><![CDATA[<b>Tomorrow</b> will be rainy.]]></description>
    <pubDate>Thu, 31 Jul 2026 00:00:00 GMT</pubDate>
  </item>
</channel></rss>"""
        with patch.object(
            claude_gateway_mcp,
            "_fetch_text",
            return_value=rss,
        ):
            with patch.dict(
                os.environ,
                {
                    claude_gateway_mcp.HOSTED_SEARCH_BASE_URL_ENV: "",
                    claude_gateway_mcp.HOSTED_SEARCH_TOKEN_ENV: "",
                    claude_gateway_mcp.HOSTED_SEARCH_MODEL_ENV: "",
                },
                clear=False,
            ):
                result = claude_gateway_mcp.web_search(
                    {"query": "Shanghai weather tomorrow", "max_results": 3}
                )

        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["backend"], "bing_rss")
        self.assertEqual(
            result["results"][0],
            {
                "title": "Weather & forecast",
                "url": "https://example.com/weather",
                "summary": "Tomorrow will be rainy.",
                "published": "Thu, 31 Jul 2026 00:00:00 GMT",
            },
        )

    def test_web_search_prefers_hosted_responses_results(self) -> None:
        hosted_response = {
            "model": "gpt-5.6-sol",
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "type": "search",
                        "sources": [
                            {
                                "title": "OpenAI Web search",
                                "url": "https://developers.openai.com/search",
                            },
                            {
                                "title": "Duplicate",
                                "url": "https://developers.openai.com/search",
                            },
                        ],
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Official search documentation.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "title": "API reference",
                                    "url": "https://developers.openai.com/reference",
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        environment = {
            claude_gateway_mcp.HOSTED_SEARCH_BASE_URL_ENV: "http://127.0.0.1:42001",
            claude_gateway_mcp.HOSTED_SEARCH_TOKEN_ENV: "local-token",
            claude_gateway_mcp.HOSTED_SEARCH_MODEL_ENV: "claude-fable-5",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(
                claude_gateway_mcp,
                "_post_hosted_response",
                return_value=hosted_response,
            ) as post,
            patch.object(claude_gateway_mcp, "_fetch_text") as fetch_text,
        ):
            result = claude_gateway_mcp.web_search(
                {"query": "OpenAI web search", "max_results": 5}
            )

        self.assertEqual(result["backend"], "hosted")
        self.assertEqual(result["model"], "gpt-5.6-sol")
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["answer"], "Official search documentation.")
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:42001")
        self.assertEqual(post.call_args.args[1], "local-token")
        self.assertEqual(post.call_args.args[2]["model"], "claude-fable-5")
        fetch_text.assert_not_called()

    def test_hosted_response_url_accepts_exact_auth_shim_endpoint(self) -> None:
        self.assertEqual(
            claude_gateway_mcp._hosted_responses_url(
                "http://127.0.0.1:42001/responses"
            ),
            "http://127.0.0.1:42001/responses",
        )

    def test_hosted_response_without_search_call_falls_back_to_bing(self) -> None:
        rss = """<rss><channel><item>
<title>Fallback result</title><link>https://example.com/fallback</link>
<description>Fallback summary</description>
</item></channel></rss>"""
        environment = {
            claude_gateway_mcp.HOSTED_SEARCH_BASE_URL_ENV: "http://127.0.0.1:42001",
            claude_gateway_mcp.HOSTED_SEARCH_TOKEN_ENV: "local-token",
            claude_gateway_mcp.HOSTED_SEARCH_MODEL_ENV: "claude-fable-5",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(
                claude_gateway_mcp,
                "_post_hosted_response",
                return_value={
                    "model": "gpt-5.5",
                    "output": [{"type": "message", "content": []}],
                },
            ),
            patch.object(claude_gateway_mcp, "_fetch_text", return_value=rss),
        ):
            result = claude_gateway_mcp.web_search({"query": "fallback query"})

        self.assertEqual(result["backend"], "bing_rss")
        self.assertEqual(result["fallback_reason"], "hosted_no_web_search_call")
        self.assertEqual(result["result_count"], 1)

    def test_non_loopback_hosted_config_falls_back_without_sending_token(self) -> None:
        rss = """<rss><channel><item>
<title>Safe result</title><link>https://example.com/safe</link>
</item></channel></rss>"""
        environment = {
            claude_gateway_mcp.HOSTED_SEARCH_BASE_URL_ENV: "https://example.com/v1",
            claude_gateway_mcp.HOSTED_SEARCH_TOKEN_ENV: "must-not-leave-host",
            claude_gateway_mcp.HOSTED_SEARCH_MODEL_ENV: "gpt-test",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(claude_gateway_mcp, "_post_hosted_response") as post,
            patch.object(claude_gateway_mcp, "_fetch_text", return_value=rss),
        ):
            result = claude_gateway_mcp.web_search({"query": "safe fallback"})

        self.assertEqual(result["backend"], "bing_rss")
        self.assertEqual(result["fallback_reason"], "hosted_config_not_loopback")
        post.assert_not_called()

    def test_hosted_http_error_falls_back_without_error_body_or_token(self) -> None:
        rss = """<rss><channel><item>
<title>Fallback result</title><link>https://example.com/fallback</link>
</item></channel></rss>"""
        environment = {
            claude_gateway_mcp.HOSTED_SEARCH_BASE_URL_ENV: "http://localhost:42001",
            claude_gateway_mcp.HOSTED_SEARCH_TOKEN_ENV: "local-secret-token",
            claude_gateway_mcp.HOSTED_SEARCH_MODEL_ENV: "gpt-test",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(
                claude_gateway_mcp,
                "_post_hosted_response",
                side_effect=claude_gateway_mcp.HostedSearchUnavailable(
                    "hosted_http_503"
                ),
            ),
            patch.object(claude_gateway_mcp, "_fetch_text", return_value=rss),
        ):
            result = claude_gateway_mcp.web_search({"query": "fallback query"})

        serialized = json.dumps(result)
        self.assertEqual(result["fallback_reason"], "hosted_http_503")
        self.assertNotIn("local-secret-token", serialized)

    def test_get_weather_returns_date_aligned_forecast(self) -> None:
        geocoding = {
            "results": [
                {
                    "name": "上海",
                    "admin1": "上海",
                    "country": "中国",
                    "latitude": 31.22222,
                    "longitude": 121.45806,
                    "timezone": "Asia/Shanghai",
                }
            ]
        }
        forecast = {
            "daily": {
                "time": ["2026-07-31", "2026-08-01"],
                "weather_code": [2, 95],
                "temperature_2m_max": [39.1, 39.5],
                "temperature_2m_min": [29.3, 29.2],
                "precipitation_probability_max": [57, 80],
                "wind_speed_10m_max": [16.0, 20.1],
                "sunrise": ["2026-07-31T05:08", "2026-08-01T05:09"],
                "sunset": ["2026-07-31T18:50", "2026-08-01T18:49"],
            }
        }
        with patch.object(
            claude_gateway_mcp,
            "_fetch_json",
            side_effect=[geocoding, forecast],
        ):
            result = claude_gateway_mcp.get_weather(
                {"location": "上海", "forecast_days": 2}
            )

        self.assertEqual(result["provider"], "Open-Meteo")
        self.assertEqual(result["resolved_location"]["timezone"], "Asia/Shanghai")
        tomorrow = result["daily"][1]
        self.assertEqual(tomorrow["date"], "2026-08-01")
        self.assertEqual(tomorrow["conditions"], "thunderstorm")
        self.assertEqual(tomorrow["precipitation_probability_max_percent"], 80)

    def test_tool_call_errors_are_returned_as_mcp_errors(self) -> None:
        response = claude_gateway_mcp._handle_request(
            {
                "jsonrpc": "2.0",
                "id": "bad",
                "method": "tools/call",
                "params": {
                    "name": "get_weather",
                    "arguments": {"location": ""},
                },
            }
        )

        self.assertIsNotNone(response)
        result = response["result"]  # type: ignore[index]
        self.assertTrue(result["isError"])
        self.assertIn("location", result["content"][0]["text"])

    def test_weather_tool_result_is_valid_utf8_json_text(self) -> None:
        with patch.object(
            claude_gateway_mcp,
            "get_weather",
            return_value={"resolved_location": {"name": "上海"}},
        ):
            response = claude_gateway_mcp._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "get_weather",
                        "arguments": {"location": "上海"},
                    },
                }
            )

        text = response["result"]["content"][0]["text"]  # type: ignore[index]
        self.assertEqual(
            json.loads(text),
            {"resolved_location": {"name": "上海"}},
        )
