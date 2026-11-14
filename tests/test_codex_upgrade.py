from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import apiagent


@unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "PowerShell 7 required")
class CodexUpgradeScriptTests(unittest.TestCase):
    def run_update(self, mode: str) -> tuple[subprocess.CompletedProcess[str], list[dict], bool]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requests = root / "requests.jsonl"
            marker = root / "installed.txt"
            environment = os.environ.copy()
            environment.update(
                APICODEX_TEST_MODE=mode,
                APICODEX_TEST_REQUESTS=str(requests),
                APICODEX_TEST_MARKER=str(marker),
                # An inherited release pin must not prevent a latest update.
                CODEX_RELEASE="0.1.0",
            )
            bootstrap = r"""
function Invoke-WebRequest {
    param([string]$Uri, [string]$OutFile, [int]$TimeoutSec)
    @{ Uri = $Uri; Path = $OutFile } | ConvertTo-Json -Compress |
        Add-Content -LiteralPath $env:APICODEX_TEST_REQUESTS
    if ($env:APICODEX_TEST_MODE -eq 'download-error' -or
        ($env:APICODEX_TEST_MODE -eq 'fallback' -and $Uri -like 'https://releases.openai.com/*')) {
        # Also exercise cleanup of a partially downloaded installer.
        Set-Content -LiteralPath $OutFile -Value 'partial download'
        throw 'HTTP 404 test failure'
    }
    $payload = @'
param([string]$Release)
if ($Release -ne 'latest') { throw 'Expected the latest release' }
if ($env:CODEX_NON_INTERACTIVE -ne '1') { throw 'Expected noninteractive update' }
if ($env:APICODEX_TEST_MODE -eq 'install-error') { throw 'Test installation failure' }
[IO.File]::WriteAllText($env:APICODEX_TEST_MARKER, $PSCommandPath)
'@
    Set-Content -LiteralPath $OutFile -Value $payload -Encoding utf8
}
"""
            result = subprocess.run(
                [shutil.which("pwsh"), "-NoProfile", "-Command", bootstrap + apiagent.CODEX_INSTALL_SCRIPT],
                env=environment,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
            )
            calls = [json.loads(line) for line in requests.read_text(encoding="utf-8-sig").splitlines()]
            installed = marker.is_file()
            # The exact temporary path is recorded by the fake downloader.
            for call in calls:
                self.assertFalse(Path(call["Path"]).exists(), result.stderr)
            return result, calls, installed

    def test_primary_download_executes_latest_installer(self) -> None:
        result, calls, installed = self.run_update("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(installed)
        self.assertEqual([call["Uri"] for call in calls], ["https://releases.openai.com/codex/install.ps1"])

    def test_download_failure_uses_official_github_fallback(self) -> None:
        result, calls, installed = self.run_update("fallback")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(installed)
        self.assertEqual(
            [call["Uri"] for call in calls],
            [
                "https://releases.openai.com/codex/install.ps1",
                "https://raw.githubusercontent.com/openai/codex/main/scripts/install/install.ps1",
            ],
        )

    def test_both_download_failures_return_nonzero(self) -> None:
        result, calls, installed = self.run_update("download-error")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        self.assertFalse(installed)
        self.assertIn("Codex update failed", result.stderr)

    def test_installation_failure_returns_nonzero_without_retry(self) -> None:
        result, calls, installed = self.run_update("install-error")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertFalse(installed)
        self.assertIn("Test installation failure", result.stderr)
