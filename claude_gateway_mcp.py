"""Small, dependency-free MCP server for Claude gateway Desktop nodes.

Claude Code intentionally disables its Anthropic-hosted WebSearch tool when the
active inference provider is ``gateway``. This stdio MCP server supplies two
local alternatives without storing another upstream API credential:

* ``web_search`` prefers the node gateway's hosted Responses web search and
  falls back to Bing's public RSS search response.
* ``get_weather`` uses Open-Meteo geocoding and forecast APIs.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from html.parser import HTMLParser
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from urllib.parse import urlsplit
from xml.etree import ElementTree


SERVER_NAME = "apiclaude-web"
SERVER_VERSION = "1.1.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 30
HOSTED_SEARCH_TIMEOUT_SECONDS = 25
HOSTED_SEARCH_BASE_URL_ENV = "APICLAUDE_WEB_SEARCH_BASE_URL"
HOSTED_SEARCH_TOKEN_ENV = "APICLAUDE_WEB_SEARCH_TOKEN"
HOSTED_SEARCH_MODEL_ENV = "APICLAUDE_WEB_SEARCH_MODEL"
_PROTOCOL_VERSION_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_WEATHER_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def _plain_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(html.unescape(value))
    return parser.text()


def _read_limited(response: Any) -> bytes:
    data = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    if len(data) > MAX_HTTP_RESPONSE_BYTES:
        raise ValueError("remote response exceeded the 2 MiB safety limit")
    return data


def _request(url: str) -> urllib_request.Request:
    return urllib_request.Request(
        url,
        headers={
            "Accept": "application/json, application/xml, text/xml;q=0.9",
            "User-Agent": "ApiClaude-Gateway-MCP/1.0",
        },
        method="GET",
    )


def _fetch_json(url: str) -> dict[str, Any]:
    with urllib_request.urlopen(
        _request(url),
        timeout=HTTP_TIMEOUT_SECONDS,
    ) as response:
        value = json.loads(_read_limited(response).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("remote endpoint returned a non-object JSON value")
    return value


def _fetch_text(url: str) -> str:
    with urllib_request.urlopen(
        _request(url),
        timeout=HTTP_TIMEOUT_SECONDS,
    ) as response:
        raw = _read_limited(response)
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


class HostedSearchUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _search_arguments(arguments: dict[str, Any]) -> tuple[str, int]:
    query = str(arguments.get("query") or "").strip()
    if len(query) < 2:
        raise ValueError("query must contain at least 2 characters")
    if len(query) > 500:
        raise ValueError("query must not exceed 500 characters")
    try:
        max_results = int(arguments.get("max_results", 6))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_results must be an integer") from exc
    if not 1 <= max_results <= 10:
        raise ValueError("max_results must be between 1 and 10")
    return query, max_results


def _bing_web_search(query: str, max_results: int) -> dict[str, Any]:

    url = "https://www.bing.com/search?" + urllib_parse.urlencode(
        {
            "q": query,
            "format": "rss",
            "setlang": "zh-Hans",
            "adlt": "strict",
        }
    )
    root = ElementTree.fromstring(_fetch_text(url))
    results: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        title = _plain_text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        summary = _plain_text(item.findtext("description") or "")
        if not title or not link.startswith(("https://", "http://")):
            continue
        result = {"title": title, "url": link}
        if summary:
            result["summary"] = summary[:1000]
        published = (item.findtext("pubDate") or "").strip()
        if published:
            result["published"] = published
        results.append(result)
        if len(results) >= max_results:
            break
    return {
        "query": query,
        "provider": "Bing RSS",
        "backend": "bing_rss",
        "results": results,
        "result_count": len(results),
    }


def _hosted_search_config() -> tuple[str, str, str] | None:
    base_url = os.environ.get(HOSTED_SEARCH_BASE_URL_ENV, "").strip()
    token = os.environ.get(HOSTED_SEARCH_TOKEN_ENV, "").strip()
    model = os.environ.get(HOSTED_SEARCH_MODEL_ENV, "").strip()
    if not base_url and not token and not model:
        return None
    if not base_url or not token or not model:
        raise HostedSearchUnavailable("hosted_config_incomplete")

    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise HostedSearchUnavailable("hosted_config_not_loopback")
    normalized = base_url.rstrip("/")
    return normalized, token, model


def _hosted_responses_url(base_url: str) -> str:
    path = urlsplit(base_url).path.rstrip("/")
    if path.endswith("/responses"):
        return base_url
    if path.endswith("/v1"):
        return f"{base_url}/responses"
    return f"{base_url}/v1/responses"


def _post_hosted_response(
    base_url: str,
    token: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request = urllib_request.Request(
        _hosted_responses_url(base_url),
        data=json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ApiClaude-Gateway-MCP/1.1",
        },
        method="POST",
    )
    opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=HOSTED_SEARCH_TIMEOUT_SECONDS) as response:
            try:
                raw = _read_limited(response)
            except ValueError as exc:
                raise HostedSearchUnavailable("hosted_response_too_large") from exc
    except urllib_error.HTTPError as exc:
        try:
            exc.read(MAX_HTTP_RESPONSE_BYTES + 1)
        except OSError:
            pass
        finally:
            exc.close()
        raise HostedSearchUnavailable(f"hosted_http_{exc.code}") from exc
    except (OSError, TimeoutError, urllib_error.URLError) as exc:
        raise HostedSearchUnavailable("hosted_connection_error") from exc

    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HostedSearchUnavailable("hosted_invalid_json") from exc
    if not isinstance(value, dict):
        raise HostedSearchUnavailable("hosted_invalid_response")
    return value


def _hosted_source(source: Any) -> dict[str, str] | None:
    if not isinstance(source, dict):
        return None
    url = str(source.get("url") or "").strip()
    if not url.startswith(("https://", "http://")):
        return None
    result = {"url": url}
    title = str(source.get("title") or "").strip()
    if title:
        result["title"] = title
    return result


def _hosted_web_search(
    query: str,
    max_results: int,
    config: tuple[str, str, str],
) -> dict[str, Any]:
    base_url, token, model = config
    response = _post_hosted_response(
        base_url,
        token,
        {
            "model": model,
            "input": (
                "Search the public web for the query below. Return a concise, "
                "source-grounded answer and cite the URLs used.\n\nQuery: " + query
            ),
            "tools": [
                {
                    "type": "web_search",
                    "external_web_access": True,
                    "search_context_size": "low",
                }
            ],
            "tool_choice": "required",
            "reasoning": {"effort": "low"},
            "include": ["web_search_call.action.sources"],
            "max_output_tokens": 1200,
            "store": False,
        },
    )
    output = response.get("output")
    if not isinstance(output, list):
        raise HostedSearchUnavailable("hosted_invalid_output")

    web_calls = [
        item
        for item in output
        if isinstance(item, dict) and item.get("type") == "web_search_call"
    ]
    if not web_calls:
        raise HostedSearchUnavailable("hosted_no_web_search_call")

    answer_parts: list[str] = []
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    def add_source(value: Any) -> None:
        source = _hosted_source(value)
        if source is None or source["url"] in seen_urls:
            return
        seen_urls.add(source["url"])
        sources.append(source)

    for call in web_calls:
        action = call.get("action")
        if isinstance(action, dict):
            for source in action.get("sources") or []:
                add_source(source)
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                answer_parts.append(text.strip())
            for annotation in block.get("annotations") or []:
                if isinstance(annotation, dict) and annotation.get("type") == "url_citation":
                    add_source(annotation)

    return {
        "query": query,
        "provider": "Responses hosted web_search",
        "backend": "hosted",
        "model": str(response.get("model") or model),
        "answer": "\n".join(answer_parts),
        "results": sources[:max_results],
        "result_count": min(len(sources), max_results),
        "source_count": len(sources),
    }


def web_search(arguments: dict[str, Any]) -> dict[str, Any]:
    query, max_results = _search_arguments(arguments)
    fallback_reason = ""
    try:
        hosted_config = _hosted_search_config()
        if hosted_config is not None:
            return _hosted_web_search(query, max_results, hosted_config)
    except HostedSearchUnavailable as exc:
        fallback_reason = exc.reason

    result = _bing_web_search(query, max_results)
    if fallback_reason:
        result["fallback_from"] = "Responses hosted web_search"
        result["fallback_reason"] = fallback_reason
    return result


def _daily_value(daily: dict[str, Any], field: str, index: int) -> Any:
    values = daily.get(field)
    if not isinstance(values, list) or index >= len(values):
        return None
    return values[index]


def get_weather(arguments: dict[str, Any]) -> dict[str, Any]:
    location = str(arguments.get("location") or "").strip()
    if len(location) < 2:
        raise ValueError("location must contain at least 2 characters")
    if len(location) > 200:
        raise ValueError("location must not exceed 200 characters")
    try:
        forecast_days = int(arguments.get("forecast_days", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("forecast_days must be an integer") from exc
    if not 1 <= forecast_days <= 7:
        raise ValueError("forecast_days must be between 1 and 7")

    geocoding_url = (
        "https://geocoding-api.open-meteo.com/v1/search?"
        + urllib_parse.urlencode(
            {
                "name": location,
                "count": 1,
                "language": "zh",
                "format": "json",
            }
        )
    )
    geocoding = _fetch_json(geocoding_url)
    places = geocoding.get("results")
    if not isinstance(places, list) or not places or not isinstance(places[0], dict):
        raise ValueError(f"no weather location matched {location!r}")
    place = places[0]
    latitude = float(place["latitude"])
    longitude = float(place["longitude"])
    timezone = str(place.get("timezone") or "auto")

    forecast_url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urllib_parse.urlencode(
            {
                "latitude": latitude,
                "longitude": longitude,
                "daily": ",".join(
                    (
                        "weather_code",
                        "temperature_2m_max",
                        "temperature_2m_min",
                        "precipitation_probability_max",
                        "wind_speed_10m_max",
                        "sunrise",
                        "sunset",
                    )
                ),
                "timezone": timezone,
                "forecast_days": forecast_days,
            }
        )
    )
    forecast = _fetch_json(forecast_url)
    daily = forecast.get("daily")
    if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
        raise ValueError("weather endpoint returned no daily forecast")

    days: list[dict[str, Any]] = []
    for index, date in enumerate(daily["time"]):
        code_value = _daily_value(daily, "weather_code", index)
        try:
            weather_code = int(code_value) if code_value is not None else None
        except (TypeError, ValueError):
            weather_code = None
        days.append(
            {
                "date": date,
                "weather_code": weather_code,
                "conditions": _WEATHER_CODES.get(weather_code, "unknown"),
                "temperature_max_c": _daily_value(
                    daily, "temperature_2m_max", index
                ),
                "temperature_min_c": _daily_value(
                    daily, "temperature_2m_min", index
                ),
                "precipitation_probability_max_percent": _daily_value(
                    daily, "precipitation_probability_max", index
                ),
                "wind_speed_max_kmh": _daily_value(
                    daily, "wind_speed_10m_max", index
                ),
                "sunrise": _daily_value(daily, "sunrise", index),
                "sunset": _daily_value(daily, "sunset", index),
            }
        )

    return {
        "requested_location": location,
        "resolved_location": {
            "name": place.get("name"),
            "admin1": place.get("admin1"),
            "country": place.get("country"),
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone,
        },
        "provider": "Open-Meteo",
        "daily": days,
    }


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "web_search",
            "description": (
                "Search the public web for current information. Use this instead of "
                "the unavailable built-in WebSearch tool on gateway deployments. "
                "Return and cite the result URLs used in the answer."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Specific search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 6,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "openWorldHint": True,
            },
        },
        {
            "name": "get_weather",
            "description": (
                "Get a current 1-7 day daily weather forecast for a city or place. "
                "Use this for weather questions instead of estimating from climate."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City or place name, for example 上海 or London.",
                    },
                    "forecast_days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 7,
                        "default": 3,
                    },
                },
                "required": ["location"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "openWorldHint": True,
            },
        },
    ]


def _tool_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            }
        ]
    }


def _handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None

    try:
        if method == "initialize":
            requested = str(
                (message.get("params") or {}).get("protocolVersion") or ""
            )
            protocol_version = (
                requested
                if _PROTOCOL_VERSION_PATTERN.fullmatch(requested)
                else DEFAULT_PROTOCOL_VERSION
            )
            result = {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_definitions()}
        elif method == "tools/call":
            params = message.get("params") or {}
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            name = params.get("name")
            if name == "web_search":
                result = _tool_result(web_search(arguments))
            elif name == "get_weather":
                result = _tool_result(get_weather(arguments))
            else:
                raise ValueError(f"unknown tool: {name}")
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
    except Exception as exc:
        result = {
            "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            "isError": True,
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    for raw_line in sys.stdin.buffer:
        try:
            message = json.loads(raw_line.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC message must be an object")
            response = _handle_request(message)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc}"},
            }
        if response is not None:
            encoded = json.dumps(
                response,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            sys.stdout.buffer.write(encoded + b"\n")
            sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
