from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_history_images as history_images
from codex_history_images import (
    index_path_for_home,
    repair_codex_history_images,
)


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")


def clipboard_path(temp_root: Path, suffix: str = "0001", extension: str = "png") -> Path:
    return temp_root / f"codex-clipboard-12345678-1234-1234-1234-12345678{suffix}.{extension}"


def response_row(images: list[tuple[Path, str]] | None = None, *, raw_content=None) -> str:
    content: list[dict[str, object]] = []
    if raw_content is not None:
        content = raw_content
    else:
        for index, (path, data_url) in enumerate(images or [], start=1):
            content.extend(
                [
                    {
                        "type": "input_text",
                        "text": f'<image name=[Image #{index}] path="{path}">',
                    },
                    {"type": "input_image", "image_url": data_url, "detail": "high"},
                    {"type": "input_text", "text": "</image>"},
                ]
            )
    return json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": content,
            },
        },
        separators=(",", ":"),
    )


def write_session(home: Path, lines: list[str], name: str = "rollout.jsonl") -> Path:
    path = home / "sessions" / "2026" / "01" / "01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CodexHistoryImageTests(unittest.TestCase):
    def test_restores_valid_image_without_modifying_session_or_account_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".codex"
            temp_root = root / "Temp"
            state_root = root / "tool-state"
            temp_root.mkdir()
            target = clipboard_path(temp_root)
            session = write_session(home, [response_row([(target, PNG_URL)])])
            sentinels = {
                home / "auth.json": b'{"token":"unchanged"}',
                home / "config.toml": b'model = "unchanged"\n',
                home / "state_5.sqlite": b"sqlite-sentinel",
                home / "desktop-data" / "Cookies": b"desktop-sentinel",
            }
            for path, data in sentinels.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            before_session = sha256(session)
            before_sentinels = {path: sha256(path) for path in sentinels}

            report = repair_codex_history_images(
                home,
                state_root=state_root,
                temp_root=temp_root,
            )

            self.assertEqual(report.restored, 1)
            self.assertEqual(target.read_bytes(), PNG_BYTES)
            self.assertEqual(sha256(session), before_session)
            self.assertEqual({path: sha256(path) for path in sentinels}, before_sentinels)
            self.assertFalse((home / ".apicodex").exists())
            self.assertTrue(index_path_for_home(home, state_root).is_file())
            self.assertFalse(list(temp_root.glob(".*.tmp")))

    def test_dry_run_is_read_only_and_reports_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".codex"
            temp_root = root / "Temp"
            state_root = root / "state"
            temp_root.mkdir()
            target = clipboard_path(temp_root)
            session = write_session(home, [response_row([(target, PNG_URL)])])
            before = sha256(session)

            report = repair_codex_history_images(
                home,
                state_root=state_root,
                temp_root=temp_root,
                dry_run=True,
            )

            self.assertEqual(report.recoverable, 1)
            self.assertEqual(report.restored, 0)
            self.assertFalse(target.exists())
            self.assertFalse(index_path_for_home(home, state_root).exists())
            self.assertEqual(sha256(session), before)

    def test_multiple_images_pair_only_with_immediately_preceding_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            temp_root.mkdir()
            first = clipboard_path(temp_root, "0001")
            second = clipboard_path(temp_root, "0002")
            ignored = clipboard_path(temp_root, "0003")
            content = [
                {"type": "input_text", "text": f'<image name=[Image #1] path="{first}">'},
                {"type": "input_image", "image_url": PNG_URL},
                {"type": "input_text", "text": "</image>"},
                {"type": "input_text", "text": f'<image name=[Image #2] path="{second}">'},
                {"type": "input_image", "image_url": PNG_URL},
                {"type": "input_text", "text": f'user text with <image name=[Image #3] path="{ignored}">'},
                {"type": "input_image", "image_url": PNG_URL},
            ]
            write_session(home, [response_row(raw_content=content)])

            report = repair_codex_history_images(
                home,
                state_root=root / "state",
                temp_root=temp_root,
            )

            self.assertEqual(report.restored, 2)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            self.assertFalse(ignored.exists())

    def test_rejects_outside_temp_invalid_data_mime_extension_and_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            temp_root.mkdir()
            outside = root / clipboard_path(temp_root, "0001").name
            bad_base64 = clipboard_path(temp_root, "0002")
            wrong_extension = clipboard_path(temp_root, "0003", "jpg")
            wrong_signature = clipboard_path(temp_root, "0004", "jpg")
            truncated_png = clipboard_path(temp_root, "0005")
            truncated_url = "data:image/png;base64," + base64.b64encode(
                b"\x89PNG\r\n\x1a\n"
            ).decode("ascii")
            rows = [
                response_row([(outside, PNG_URL)]),
                response_row([(bad_base64, "data:image/png;base64,not-valid!!")]),
                response_row([(wrong_extension, PNG_URL)]),
                response_row(
                    [(wrong_signature, "data:image/jpeg;base64," + base64.b64encode(PNG_BYTES).decode("ascii"))]
                ),
                response_row([(truncated_png, truncated_url)]),
            ]
            write_session(home, rows)

            report = repair_codex_history_images(
                home,
                state_root=root / "state",
                temp_root=temp_root,
            )

            self.assertGreaterEqual(report.rejected + report.skipped, 4)
            self.assertEqual(report.restored, 0)
            self.assertFalse(outside.exists())
            self.assertFalse(bad_base64.exists())
            self.assertFalse(wrong_extension.exists())
            self.assertFalse(wrong_signature.exists())
            self.assertFalse(truncated_png.exists())

    def test_protected_state_root_is_never_created_or_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".codex"
            temp_root = root / "Temp"
            protected_state = home / "repair-state"
            temp_root.mkdir()
            target = clipboard_path(temp_root)
            write_session(home, [response_row([(target, PNG_URL)])])

            report = repair_codex_history_images(
                home,
                state_root=protected_state,
                temp_root=temp_root,
                protected_roots=(home,),
            )

            self.assertEqual(report.restored, 1)
            self.assertGreaterEqual(report.errors, 1)
            self.assertFalse(protected_state.exists())
            self.assertTrue(target.is_file())

    def test_restore_refuses_temp_directory_identity_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temp_root = root / "Temp"
            temp_root.mkdir()
            target = clipboard_path(temp_root)
            identity = history_images._directory_identity(temp_root)
            self.assertIsNotNone(identity)
            assert identity is not None

            with patch.object(
                history_images,
                "_directory_identity",
                return_value=(identity[0] + 1, identity[1]),
            ):
                with self.assertRaises(OSError):
                    history_images._write_new_file(
                        target,
                        PNG_BYTES,
                        temp_root=temp_root,
                        temp_identity=identity,
                    )

            self.assertFalse(target.exists())
            self.assertEqual(list(temp_root.iterdir()), [])

    def test_rejects_parent_traversal_and_non_clipboard_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            temp_root.mkdir()
            traversal = temp_root / "nested" / ".." / clipboard_path(temp_root, "0001").name
            ordinary = temp_root / "ordinary.png"
            write_session(home, [response_row([(traversal, PNG_URL), (ordinary, PNG_URL)])])

            report = repair_codex_history_images(
                home,
                state_root=root / "state",
                temp_root=temp_root,
            )

            self.assertGreaterEqual(report.rejected + report.skipped, 2)
            self.assertEqual(report.restored, 0)

    def test_existing_matching_image_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            state_root = root / "state"
            temp_root.mkdir()
            target = clipboard_path(temp_root)
            write_session(home, [response_row([(target, PNG_URL)])])
            first = repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)
            second = repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)

            self.assertEqual(first.restored, 1)
            self.assertEqual(second.restored, 0)
            self.assertEqual(second.already_present, 1)
            self.assertEqual(second.scanned_files, 0)
            self.assertEqual(second.unchanged_files, 1)

    def test_existing_conflicting_image_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            temp_root.mkdir()
            target = clipboard_path(temp_root)
            target.write_bytes(b"do-not-overwrite")
            write_session(home, [response_row([(target, PNG_URL)])])

            report = repair_codex_history_images(
                home,
                state_root=root / "state",
                temp_root=temp_root,
            )

            self.assertEqual(report.conflicts, 1)
            self.assertEqual(report.restored, 0)
            self.assertEqual(target.read_bytes(), b"do-not-overwrite")

    def test_incremental_append_indexes_only_new_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            state_root = root / "state"
            temp_root.mkdir()
            first = clipboard_path(temp_root, "0001")
            second = clipboard_path(temp_root, "0002")
            session = write_session(home, [response_row([(first, PNG_URL)])])
            initial = repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)
            with session.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(response_row([(second, PNG_URL)]) + "\n")

            appended = repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)

            self.assertEqual(initial.restored, 1)
            self.assertEqual(appended.incremental_files, 1)
            self.assertEqual(appended.rebuilt_files, 0)
            self.assertEqual(appended.restored, 1)
            self.assertTrue(second.is_file())

    def test_scan_failure_preserves_previous_index_and_recovers_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            state_root = root / "state"
            temp_root.mkdir()
            old_target = clipboard_path(temp_root, "0001")
            new_target = clipboard_path(temp_root, "0002")
            session = write_session(home, [response_row([(old_target, PNG_URL)])])
            repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)
            old_target.unlink()
            session.write_bytes(
                (response_row([(new_target, PNG_URL)]) + '\n{"type":"noop"}\n').encode("utf-8")
            )
            index_path = index_path_for_home(home, state_root)
            before = json.loads(index_path.read_text(encoding="utf-8"))
            original_open = Path.open

            def fail_session_read(path: Path, *args, **kwargs):
                mode = args[0] if args else kwargs.get("mode", "r")
                if path == session and mode == "rb":
                    raise OSError("simulated sharing violation")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", new=fail_session_read):
                failed = repair_codex_history_images(
                    home, state_root=state_root, temp_root=temp_root
                )

            after = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(failed.errors, 1)
            self.assertEqual(
                after["files"]["2026/01/01/rollout.jsonl"]["images"],
                before["files"]["2026/01/01/rollout.jsonl"]["images"],
            )
            self.assertFalse(new_target.exists())

            recovered = repair_codex_history_images(
                home, state_root=state_root, temp_root=temp_root
            )
            self.assertEqual(recovered.restored, 1)
            self.assertTrue(new_target.is_file())

    def test_same_size_and_mtime_content_change_rebuilds_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            state_root = root / "state"
            temp_root.mkdir()
            old_target = clipboard_path(temp_root, "0001")
            new_target = clipboard_path(temp_root, "0002")
            session = write_session(home, [response_row([(old_target, PNG_URL)])])
            repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)
            old_target.unlink()
            original_stat = session.stat()
            replacement = (response_row([(new_target, PNG_URL)]) + "\n").encode("utf-8")
            self.assertEqual(len(replacement), original_stat.st_size)
            session.write_bytes(replacement)
            os.utime(
                session,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            current_stat = session.stat()
            self.assertEqual(current_stat.st_size, original_stat.st_size)
            self.assertEqual(current_stat.st_mtime_ns, original_stat.st_mtime_ns)

            report = repair_codex_history_images(
                home, state_root=state_root, temp_root=temp_root
            )

            self.assertEqual(report.unchanged_files, 0)
            self.assertEqual(report.rebuilt_files, 1)
            self.assertEqual(report.restored, 1)
            self.assertTrue(new_target.is_file())

    def test_duplicate_target_falls_back_to_next_valid_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            state_root = root / "state"
            temp_root.mkdir()
            target = clipboard_path(temp_root, "0001")
            write_session(home, [response_row([(target, PNG_URL)])], name="a.jsonl")
            write_session(home, [response_row([(target, PNG_URL)])], name="b.jsonl")
            repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)
            target.unlink()

            index_path = index_path_for_home(home, state_root)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            # Make the first duplicate candidate stale while leaving the
            # independently embedded copy in b.jsonl usable.
            index["files"]["2026/01/01/a.jsonl"]["images"][0]["lineOffset"] = 1
            index_path.write_text(
                json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            report = repair_codex_history_images(
                home, state_root=state_root, temp_root=temp_root
            )

            self.assertEqual(report.restored, 1)
            self.assertEqual(report.stale, 0)
            self.assertTrue(target.is_file())

    def test_truncated_session_rebuilds_and_drops_old_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            state_root = root / "state"
            temp_root.mkdir()
            old_target = clipboard_path(temp_root, "0001")
            new_target = clipboard_path(temp_root, "0002")
            session = write_session(home, [response_row([(old_target, PNG_URL)])])
            repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)
            old_target.unlink()
            session.write_text(response_row([(new_target, PNG_URL)]) + "\n", encoding="utf-8", newline="\n")
            os.utime(session, None)

            report = repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)

            self.assertEqual(report.rebuilt_files, 1)
            self.assertFalse(old_target.exists())
            self.assertTrue(new_target.is_file())

    def test_corrupt_and_partial_jsonl_do_not_block_other_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            temp_root = root / "Temp"
            state_root = root / "state"
            temp_root.mkdir()
            first = clipboard_path(temp_root, "0001")
            second = clipboard_path(temp_root, "0002")
            session = write_session(
                home,
                [
                    '{"type":"response_item","input_image":',
                    response_row([(first, PNG_URL)]),
                ],
            )
            with session.open("ab") as handle:
                handle.write(response_row([(second, PNG_URL)]).encode("utf-8")[:40])

            report = repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)

            self.assertTrue(first.is_file())
            self.assertFalse(second.exists())
            self.assertGreaterEqual(report.rejected, 1)

            with session.open("ab") as handle:
                full = response_row([(second, PNG_URL)]).encode("utf-8")
                handle.write(full[40:] + b"\n")
            resumed = repair_codex_history_images(home, state_root=state_root, temp_root=temp_root)
            self.assertEqual(resumed.incremental_files, 1)
            self.assertTrue(second.is_file())


if __name__ == "__main__":
    unittest.main()
