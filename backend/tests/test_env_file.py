import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm.env_file import load_env_file
from llm.settings import GeminiSettings


class EnvFileTests(unittest.TestCase):
    def test_loads_key_for_settings_without_exposing_it(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / '.env'
            path.write_text('# Gemini\nexport GEMINI_API_KEY="test-secret" # note\nGEMINI_MODEL=gemini-test\n')
            load_env_file(path)
            settings = GeminiSettings(Path(tmp) / 'settings.json')
            self.assertEqual(settings.read(), ('test-secret', 'gemini-test'))
            self.assertNotIn('test-secret', str(settings.public()))

    def test_existing_environment_wins_and_unrelated_settings_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'GEMINI_API_KEY': 'existing'}, clear=True):
            path = Path(tmp) / '.env'
            path.write_text('GEMINI_API_KEY=new\nUNRELATED_SETTING=ignored\n')
            load_env_file(path)
            self.assertEqual(os.environ['GEMINI_API_KEY'], 'existing')
            self.assertNotIn('UNRELATED_SETTING', os.environ)

    def test_no_shell_expansion_or_partial_load_on_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / '.env'
            load_env_file(path)
            path.write_text('GEMINI_API_KEY=$(literal)\n')
            load_env_file(path)
            self.assertEqual(os.environ.pop('GEMINI_API_KEY'), '$(literal)')
            path.write_text('GEMINI_API_KEY=test-secret\nGEMINI_MODEL="unclosed\n')
            with self.assertRaisesRegex(ValueError, 'line 2') as error:
                load_env_file(path)
            self.assertNotIn('test-secret', str(error.exception))
            self.assertNotIn('GEMINI_API_KEY', os.environ)
