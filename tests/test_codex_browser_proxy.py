"""System proxy inheritance through Desktop's regenerated browser MCP config."""
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import codex_native_audit as audit


class BrowserProxyTests(unittest.TestCase):
    def request(self, method="thread/resume", server=None, nested=False):
        server = server if server is not None else {
            "command": "official-node_repl.exe", "env_vars": ["CODEX_WINDOWS_REGISTERED_CORE"],
            "env": {"CODEX_HOME": "account-home", "NODE_REPL_TRUSTED_CODE_PATHS": "original"},
        }
        config = {"mcp_servers": {"node_repl": server}} if nested else {"mcp_servers.node_repl": server}
        return {"id": 4, "method": method, "params": {
            "threadId": "existing-thread", "config": config, "input": "原始文字 PRIVATE_INPUT"}}

    def test_system_proxy_refreshes_stale_environment_without_changing_credentials(self):
        original = {"HTTPS_PROXY": "http://old:1", "https_proxy": "http://old:1",
                    "APICODEX_API_KEY": "synthetic-secret", "NO_PROXY": "custom.internal"}
        with patch.object(audit.sys, "platform", "win32"), patch.object(
                audit.urllib.request, "getproxies_registry", create=True,
                return_value={"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}):
            result = audit._desktop_proxy_environment(original)
        self.assertEqual(result["HTTP_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(result["HTTPS_PROXY"], result["https_proxy"])
        self.assertEqual(result["APICODEX_API_KEY"], "synthetic-secret")
        self.assertEqual(result["NO_PROXY"], "custom.internal,localhost,127.0.0.1,::1")
        self.assertEqual(original["HTTPS_PROXY"], "http://old:1")

    def test_no_windows_proxy_keeps_explicit_environment_and_other_platforms_do_not_read_registry(self):
        original = {"HTTPS_PROXY": "http://explicit:2", "NO_PROXY": "custom"}
        with patch.object(audit.sys, "platform", "win32"), patch.object(
                audit.urllib.request, "getproxies_registry", create=True, return_value={}):
            self.assertEqual(audit._desktop_proxy_environment(original), original)
        with patch.object(audit.sys, "platform", "darwin"), patch.object(
                audit.urllib.request, "getproxies_registry", create=True) as registry:
            self.assertEqual(audit._desktop_proxy_environment(original), original)
            registry.assert_not_called()

    def test_generated_config_retains_proxy_names_on_start_resume_and_fork(self):
        for method in ("thread/start", "thread/resume", "thread/fork"):
            for nested in (False, True):
                with self.subTest(method=method, nested=nested):
                    request = self.request(method, nested=nested)
                    line = (json.dumps(request, ensure_ascii=False) + "\n").encode()
                    actual = json.loads(audit._desktop_proxy_request(line, ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")))
                    server = actual["params"]["config"]["mcp_servers"]["node_repl"] if nested else actual["params"]["config"]["mcp_servers.node_repl"]
                    expected = request["params"]["config"]["mcp_servers"]["node_repl"] if nested else request["params"]["config"]["mcp_servers.node_repl"]
                    self.assertEqual(server.pop("env_vars"), ["CODEX_WINDOWS_REGISTERED_CORE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"])
                    expected.pop("env_vars")
                    self.assertEqual(actual, request)
                    self.assertEqual(server, expected)

    def test_explicit_proxy_and_network_security_settings_are_preserved(self):
        server = {"command": "official-node_repl.exe", "env_vars": ["HTTPS_PROXY"],
                  "env": {"HTTPS_PROXY": "http://explicit:3", "BROWSER_USE_DISABLE_AMBIENT_NETWORK": "1"}}
        line = json.dumps(self.request(server=server)).encode()
        actual = json.loads(audit._desktop_proxy_request(line, ("HTTP_PROXY", "HTTPS_PROXY")))
        updated = actual["params"]["config"]["mcp_servers.node_repl"]
        self.assertEqual(updated["env"], server["env"])
        self.assertEqual(updated["env_vars"], ["HTTPS_PROXY", "HTTP_PROXY"])
        forwarded = audit._desktop_proxy_request(line, ("HTTPS_PROXY",))
        self.assertEqual(forwarded, line)

    def test_unknown_invalid_and_unrelated_messages_remain_byte_identical(self):
        messages = [b"not-json\n", b"\xff\n", b"[]\n", b"null\n",
                    b'{"method":"turn/start","params":{"input":"PRIVATE"}}\n',
                    b'{"id":8,"result":{"decision":"accept"}}\n',
                    b'{"method":"thread/resume","params":{}}\n',
                    json.dumps(self.request(server={"url": "https://example.invalid/mcp"})).encode(),
                    json.dumps(self.request(server={"command": "node", "env_vars": "invalid"})).encode()]
        for line in messages:
            with self.subTest(line=line[:80]):
                self.assertEqual(audit._desktop_proxy_request(line, ("HTTPS_PROXY",)), line)
        valid = json.dumps(self.request()).encode()
        self.assertEqual(audit._desktop_proxy_request(valid, ()), valid)

    def test_forwarding_preserves_sequence_and_closes_input_at_eof_without_logging(self):
        request = json.dumps(self.request()).encode() + b"\n"
        response = b'{"id":9,"result":{"decision":"accept"}}\n'
        class Sink(io.BytesIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()
        sink = Sink()
        with patch("sys.stderr", io.StringIO()) as err:
            audit._forward_desktop_input(io.BytesIO(request + response), sink, ("HTTPS_PROXY",))
            self.assertEqual(err.getvalue(), "")
        self.assertTrue(sink.closed)
        lines = sink.saved.splitlines()
        self.assertIn("HTTPS_PROXY", json.loads(lines[0])["params"]["config"]["mcp_servers.node_repl"]["env_vars"])
        self.assertEqual(lines[1], response.rstrip())

    def test_native_desktop_inherits_only_proxy_names_in_generated_request(self):
        with tempfile.TemporaryDirectory() as folder:
            native_env = {"HTTP_PROXY": "http://127.0.0.1:7897", "HTTPS_PROXY": "http://127.0.0.1:7897",
                          "APICODEX_API_KEY": "synthetic-secret"}
            request = json.dumps(self.request()).encode() + b"\n"
            with patch.object(audit, "_native_environment", return_value=native_env), \
                    patch.object(audit, "_desktop_proxy_environment", side_effect=lambda env: env), \
                    patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(request))), \
                    patch("subprocess.Popen") as spawn, patch.object(audit, "_forward_desktop_input") as forward:
                child = spawn.return_value
                child.stderr = io.BytesIO()
                child.wait.return_value = 0
                child.poll.return_value = 0
                self.assertEqual(audit._collected_run("native.exe", ["app-server"], home=Path(folder), client="Desktop", profile="fixture"), 0)
                forward.assert_called_once()
                self.assertEqual(forward.call_args.args[2], ("HTTP_PROXY", "HTTPS_PROXY"))
                self.assertEqual(spawn.call_args.kwargs["stdin"], audit.subprocess.PIPE)
                self.assertNotIn("synthetic-secret", str(spawn.call_args.args))
                self.assertEqual(spawn.call_args.kwargs["env"]["APICODEX_API_KEY"], "synthetic-secret")


if __name__ == "__main__":
    unittest.main()
