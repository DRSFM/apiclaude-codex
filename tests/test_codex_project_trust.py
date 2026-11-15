from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import apiagent


class ProjectTrustTests(unittest.TestCase):
    def test_existing_project_is_not_appended_with_different_quoting(self):
        cwd = Path('E:/论文/paper')
        for header in (
            f"[projects.'{cwd}']",
            f'[projects.{apiagent.toml_basic_string(str(cwd))}]',
            f"[ 'projects' . '{cwd}' ] # existing project",
        ):
            with self.subTest(header=header), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                config = home / 'config.toml'
                original = f'{header}\ntrust_level = "untrusted"\n'.encode('utf-8')
                config.write_bytes(original)
                with patch.object(apiagent.Path, 'cwd', return_value=cwd):
                    apiagent.add_current_project_trust(home)
                self.assertEqual(config.read_bytes(), original)

    def test_header_inside_multiline_value_does_not_count_as_project(self):
        cwd = Path('E:/论文/paper')
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / 'config.toml'
            original = f'description = """\n[projects.{apiagent.toml_basic_string(str(cwd))}]\n"""\n'
            config.write_text(original, encoding='utf-8')
            with patch.object(apiagent.Path, 'cwd', return_value=cwd):
                apiagent.add_current_project_trust(home)
                once = config.read_bytes()
                apiagent.add_current_project_trust(home)
            self.assertIn('trust_level = "trusted"', once.decode('utf-8'))
            self.assertEqual(config.read_bytes(), once)


if __name__ == '__main__':
    unittest.main()
