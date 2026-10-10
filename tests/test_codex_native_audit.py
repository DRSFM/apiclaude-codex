"""Privacy, association and native-launch checks for Desktop/exec audit."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import threading
import os
from unittest.mock import patch
import zlib

import codex_native_audit as audit


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.collector = audit.DiagnosticCollector(self.rows.append, profile="test-account", client="Desktop")

    def feed(self, target, message):
        return self.collector.feed(json.dumps({"timestamp": "2026-10-10T00:00:00Z", "level": "TRACE",
                                               "target": target, "fields": {"message": message}}))

    def request(self, effort="low", session="session-1"):
        payload = {"model": "gpt-5.2", "reasoning": {"effort": effort}, "stream": True,
                   "input": [{"text": "PRIVATE_INPUT_MARKER"}], "instructions": "PRIVATE_SYSTEM_MARKER",
                   "client_metadata": {"session_id": session, "turn_id": "turn-1", "secret": "PRIVATE_KEY_MARKER"}}
        self.feed(audit.TARGETS[0], "POST to https://example.invalid/v1/responses: " + json.dumps(payload))

    def response(self, rid="resp-1", effort="high", kind="response.completed"):
        response = {"id": rid, "model": "gpt-5.2", "reasoning": {"effort": effort, "summary": "PRIVATE_REASONING_MARKER"},
                    "output": [{"text": "PRIVATE_OUTPUT_MARKER"}], "headers": {"authorization": "PRIVATE_AUTH_MARKER"}}
        self.feed(audit.TARGETS[1], "SSE event: " + json.dumps({"type": kind, "response": response}))

    def test_actual_outbound_and_echo_are_distinct_and_only_metadata_is_retained(self):
        self.request()
        self.response(kind="response.created")
        self.response()
        self.assertEqual(len(self.rows), 3)
        self.assertEqual(self.rows[0]["request"]["reasoning"]["effort"], "low")
        self.assertEqual(self.rows[-1]["response"]["reasoning"]["effort"], "high")
        self.assertEqual(self.rows[0]["session_id"], "session-1")
        self.assertEqual(len({row["request_id"] for row in self.rows}), 1)
        self.assertNotIn("PRIVATE_", json.dumps(self.rows))
        self.assertNotIn("PRIVATE_", repr(self.collector.pending) + repr(self.collector.responses))

    def test_missing_echo_never_substitutes_request_effort(self):
        self.request()
        self.response(effort=None)
        self.assertNotIn("reasoning", self.rows[-1]["response"])

    def test_plain_native_diagnostic_with_ansi_colors(self):
        self.assertTrue(self.collector.feed('\x1b[2m2026-10-10T00:00:00Z\x1b[0m TRACE codex_http_client::transport: POST to https://example.invalid/responses: {"reasoning":{"effort":"low"},"stream":true}'))
        self.response()
        self.assertEqual(self.rows[0]["request"]["reasoning"]["effort"], "low")

    def test_overlapping_unbound_requests_do_not_guess_association(self):
        self.request("low", "first")
        self.request("xhigh", "second")
        self.response("second-response")
        self.response("first-response")
        requests = [row for row in self.rows if row["event_type"] == "request_sent"]
        self.assertTrue(requests)
        self.assertTrue(all("reasoning" not in row["request"] for row in requests))
        self.assertTrue(all(not row.get("session_id") for row in requests))
        self.request("medium", "third")
        self.response("third-response")
        self.assertEqual(self.rows[-2]["request"]["reasoning"]["effort"], "medium")

    def test_duplicates_are_not_fabricated_as_new_requests(self):
        self.request()
        self.response()
        self.response()
        self.assertEqual(len(self.rows), 2)

    def test_invalid_or_conflicting_efforts_are_not_invented(self):
        self.assertEqual(audit._effort({"reasoning": {"effort": ["low"]}}), {})
        self.request()
        self.response(effort="arbitrary-private-text")
        self.assertNotIn("reasoning", self.rows[-1]["response"])
        self.assertEqual(audit._effort({"reasoning": {"effort": "low"}, "reasoning_effort": "high"}),
                         {"reasoning": {"effort": "low"}, "reasoning_effort": "high"})

    def test_malformed_sensitive_diagnostic_is_suppressed_and_breaks_association(self):
        self.request()
        self.assertTrue(self.feed(audit.TARGETS[1], "SSE event: {PRIVATE_MARKER broken"))
        self.response()
        self.assertNotIn("reasoning", self.rows[0]["request"])

    def test_compressed_websocket_warmup_is_not_a_reply(self):
        encoder = zlib.compressobj(wbits=-15)
        for warmup in (True, False):
            obj = {"type": "response.create", "generate": not warmup, "model": "gpt-5.2",
                   "reasoning": {"effort": "low"}, "input": "PRIVATE_WS_INPUT_MARKER"}
            raw = encoder.compress(json.dumps(obj).encode()) + encoder.flush(zlib.Z_SYNC_FLUSH)
            raw = raw[:-4]
            literal = 'b"' + ''.join('\\x%02x' % byte for byte in raw) + '"'
            self.feed(audit.TARGETS[2], "Sending frame: Frame { header: Data(Text), rsv1: true, payload: " + literal + " }")
            self.response("warmup" if warmup else "reply")
        self.assertEqual(len(self.rows), 2)
        self.assertEqual(self.rows[-1]["response"]["id"], "reply")
        self.assertNotIn("PRIVATE_", json.dumps(self.rows))

    def test_rust_null_escape_is_not_python_octal(self):
        self.assertEqual(audit._rust_bytes(r'b"\01\xFF\n"'), bytes((0, 49, 255, 10)))

    def test_other_structured_diagnostics_do_not_forward_provider_error_body(self):
        class Process:
            stderr = io.BytesIO((json.dumps({"level": "ERROR", "target": "other::target",
                                 "fields": {"message": "PRIVATE_ERROR_BODY"}}) + "\n").encode())
        stream = io.StringIO()
        with patch("sys.stderr", stream):
            audit._drain(Process(), self.collector)
        self.assertNotIn("PRIVATE_", stream.getvalue())
        self.assertIn("diagnostic error", stream.getvalue())

    def test_writer_failure_does_not_raise_in_client(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "audits").write_text("existing unrelated file")
            writer = audit.MetadataWriter(home)
            writer.emit({"event_type": "test"})
            writer.close()
            self.assertTrue(writer.failed)
            self.assertEqual((home / "audits").read_text(), "existing unrelated file")

    def test_writer_persists_collector_metadata_only(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = audit.MetadataWriter(Path(folder))
            self.collector.emit = writer.emit
            self.request()
            self.response()
            writer.close()
            text = writer.path.read_text()
            self.assertNotIn("PRIVATE_", text)
            self.assertEqual(len(text.splitlines()), 2)

    def test_file_boundary_discards_unexpected_body_and_credentials(self):
        safe = audit._metadata_only({"event_type": "request_sent", "request_id": "request-1",
                                    "timestamp": "2026-10-10T00:00:00Z", "input": "PRIVATE_INPUT",
                                    "api_key": "PRIVATE_CREDENTIAL", "request": {"model": "gpt-5.2",
                                    "instructions": "PRIVATE_SYSTEM", "reasoning": {"effort": "low",
                                    "summary": "PRIVATE_REASONING"}}})
        self.assertNotIn("PRIVATE_", json.dumps(safe))
        self.assertEqual(safe["request"]["reasoning"], {"effort": "low"})

    def test_concurrent_same_home_writers_keep_complete_json_records(self):
        with tempfile.TemporaryDirectory() as folder:
            writers = [audit.MetadataWriter(Path(folder)) for _ in range(4)]
            def write(writer, prefix):
                for index in range(80):
                    writer.emit({"event_type": "request_sent", "request_id": f"{prefix}-{index}",
                                 "timestamp": "2026-10-10T00:00:00Z", "request": {"stream": True}})
                writer.close()
            threads = [threading.Thread(target=write, args=(writer, index)) for index, writer in enumerate(writers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertFalse(any(writer.failed for writer in writers))
            rows = [json.loads(line) for line in writers[0].path.read_text().splitlines()]
            self.assertEqual(len({row["request_id"] for row in rows}), 320)

    def test_profiles_do_not_share_pending_request_state(self):
        other_rows = []
        other = audit.DiagnosticCollector(other_rows.append, profile="another-account", client="CLI")
        self.request("low", "first")
        other.feed(json.dumps({"target": audit.TARGETS[0], "fields": {"message":
                   'POST to https://example.invalid/responses: {"reasoning":{"effort":"xhigh"},"stream":true}'}}))
        self.response("first-response", "high")
        other.feed(json.dumps({"target": audit.TARGETS[1], "fields": {"message":
                   'SSE event: {"type":"response.completed","response":{"id":"second-response","reasoning":{"effort":"xhigh"}}}'}}))
        self.assertEqual(self.rows[0]["request"]["reasoning"]["effort"], "low")
        self.assertEqual(other_rows[0]["request"]["reasoning"]["effort"], "xhigh")
        self.assertNotEqual(self.rows[0]["source_id"], other_rows[0]["source_id"])

    def test_argument_command_detection_respects_option_values_and_delimiter(self):
        for args, expected in [(["-c", "exec", "resume", "id"], "resume"),
                               (["-m", "review", "exec", "prompt"], "exec"),
                               (["--", "exec"], ""), (["prompt with exec", "review"], "prompt with exec"),
                               (["app-server", "--listen", "stdio://"], "app-server")]:
            with self.subTest(args=args):
                self.assertEqual(audit._command(args), expected)

    def test_interactive_cli_keeps_native_arguments_without_remote_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "native.exe"
            binary.write_bytes(b"test-only")
            args = ["--worktree", "--no-daemon", "-c", 'model_reasoning_effort="high"']
            context = {"APICODEX_NATIVE_AUDIT_BINARY": str(binary), "CODEX_HOME": folder}
            with patch.dict(os.environ, context), patch("subprocess.run") as run, \
                    patch("sys.stderr", io.StringIO()), patch("codex_native_audit._collected_run") as collect:
                run.return_value.returncode = 7
                self.assertEqual(audit.native_entry(args), 7)
                self.assertEqual(run.call_args.args[0], [str(binary), *args])
                collect.assert_not_called()
                self.assertNotIn("--remote", run.call_args.args[0])

    def test_exec_helper_wraps_exec_alias_only_and_leaves_management_and_tui_native(self):
        home = Path("isolated-home")
        with patch("codex_native_audit.prepare_environment", return_value={"CODEX_CLI_PATH": "audit.exe"}) as prepare:
            for args in (["exec", "--json", "-"], ["-c", "model_reasoning_effort=low", "e", "prompt"]):
                self.assertEqual(audit.exec_environment("native.exe", args, home)[0], "audit.exe")
            self.assertEqual(prepare.call_count, 2)
            for args in (["resume", "id"], ["--worktree"], ["features", "list"], ["exec", "--help"], ["review"], ["--", "exec"]):
                self.assertEqual(audit.exec_environment("native.exe", args, home), ("native.exe", {}))
            self.assertEqual(prepare.call_count, 2)

    def test_desktop_uses_its_bundled_core_and_preserves_chinese_profile_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "AppxManifest.xml").touch()
            executable = root / "app/ChatGPT.exe"
            home = root / ".codex-api/accounts/account-1"
            with patch("codex_native_audit.prepare_environment", return_value={"CODEX_CLI_PATH": "audit.exe"}) as prepare:
                env = audit.desktop_environment(executable, home)
                self.assertEqual(env["APICODEX_NATIVE_AUDIT_PACKAGED"], "1")
                self.assertEqual(prepare.call_args.args, (str(root / "app/resources/codex.exe"), home))
                self.assertEqual(prepare.call_args.kwargs["profile"], "账号/account-1")
        collector = audit.DiagnosticCollector(self.rows.append, profile="官方", client="Desktop")
        self.assertEqual(collector.profile, "官方")

    def test_audit_setup_failure_keeps_client_usable(self):
        with patch("codex_native_audit.prepare_environment", side_effect=OSError("fixture")), patch("sys.stderr", io.StringIO()):
            self.assertEqual(audit.exec_environment("native.exe", ["exec", "prompt"], Path("home")), ("native.exe", {}))
            self.assertEqual(audit.desktop_environment(Path("ChatGPT.exe"), Path("home")), {})

    def test_native_child_drops_collector_context_and_retains_auth_in_environment_only(self):
        context = {"APICODEX_NATIVE_AUDIT_BINARY": "native.exe", "APICODEX_NATIVE_AUDIT_PROFILE": "account-b",
                   "APICODEX_NATIVE_AUDIT_PACKAGED": "0", "APICODEX_API_KEY": "synthetic-secret", "CODEX_HOME": "home-b"}
        with patch.dict(os.environ, context):
            env = audit._native_environment()
            self.assertEqual(env["APICODEX_API_KEY"], "synthetic-secret")
            self.assertEqual(env["CODEX_HOME"], "home-b")
            self.assertEqual(env["CODEX_CLI_PATH"], "native.exe")
            self.assertFalse(any(k.startswith("APICODEX_NATIVE_AUDIT_") for k in env))

    def test_trace_is_scoped_to_collector_and_not_inherited_by_shell_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"APICODEX_NATIVE_AUDIT_BINARY": "native.exe"}), \
                    patch("subprocess.Popen") as spawn:
                spawn.return_value.stderr = io.BytesIO()
                spawn.return_value.wait.return_value = 0
                spawn.return_value.poll.return_value = 0
                self.assertEqual(audit._collected_run("native.exe", ["exec", "--json", "-"], home=Path(folder), client="CLI", profile="fixture"), 0)
                self.assertEqual(spawn.call_args.args[0][:3], ["native.exe", "-c", 'shell_environment_policy.set.RUST_LOG="warn"'])
                self.assertEqual(spawn.call_args.kwargs["env"]["RUST_LOG"], audit.LOG_FILTER)
