import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from llm.settings import GeminiSettings, normalize_api_key, verify_key
from llm.explain import GeminiError
from local_server import AnalysisService, LocalServer
import test_extension

KEY = 'test-key-not-a-real-credential'
MODEL = 'gemini-3.8-flash'


class SettingsTests(unittest.TestCase):
    def test_private_persistence_survives_new_service_without_environment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GOOGLE_API_KEY': ''}), patch('llm.settings.verify_key') as verify:
            path = Path(tmp) / '.detest' / 'gemini.json'
            settings = GeminiSettings(path)
            self.assertFalse(settings.public()['gemini_available'])
            result = settings.save(KEY, MODEL)
            verify.assert_called_once_with(KEY, MODEL)
            self.assertNotIn(KEY, json.dumps(result))
            self.assertEqual(GeminiSettings(path).read(), (KEY, MODEL))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_invalid_or_rejected_key_does_not_replace_working_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = GeminiSettings(Path(tmp) / 'settings.json')
            with patch('llm.settings.verify_key'):
                settings.save(KEY, MODEL)
            before = settings.path.read_bytes()
            with patch('llm.settings.verify_key', side_effect=GeminiError('Key rejected')):
                with self.assertRaises(GeminiError):
                    settings.save('replacement-test-key-abcdefgh', MODEL)
            self.assertEqual(before, settings.path.read_bytes())
            for invalid in ['<script>', 'key; command', '""', 3]:
                with self.assertRaises(ValueError):
                    settings.save(invalid, MODEL)

    def test_pasted_key_formats_are_normalized_before_provider_validation(self):
        samples = [KEY, f'"{KEY}"', f"'{KEY}'", f'“{KEY}”',
                   f'GEMINI_API_KEY="{KEY}"', f"export GOOGLE_API_KEY='{KEY}'",
                   f'```bash\nexport GEMINI_API_KEY="{KEY}"\n```',
                   '\u200b' + KEY + '\ufeff', KEY[:8] + '\n' + KEY[8:]]
        with tempfile.TemporaryDirectory() as tmp, patch('llm.settings.verify_key') as verify:
            settings = GeminiSettings(Path(tmp) / 'private' / 'gemini.json')
            for sample in samples:
                with self.subTest(format=sample.replace(KEY, '[test key]')):
                    self.assertEqual(normalize_api_key(sample), KEY)
                    settings.save(sample, MODEL)
                    verify.assert_called_with(KEY, MODEL)
                    self.assertEqual(settings.read()[0], KEY)

    def test_provider_decides_key_validity_and_empty_input_is_distinct(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GOOGLE_API_KEY': ''}):
            settings = GeminiSettings(Path(tmp) / 'private' / 'gemini.json')
            with patch('llm.settings.verify_key', side_effect=GeminiError('Key rejected')) as verify:
                with self.assertRaisesRegex(ValueError, 'field is empty'):
                    settings.save('', MODEL)
                verify.assert_not_called()
                with self.assertRaisesRegex(GeminiError, 'Key rejected'):
                    settings.save('abc', MODEL)
                verify.assert_called_once_with('abc', MODEL)
                self.assertFalse(settings.path.exists())

    def test_corrupt_settings_can_be_replaced(self):
        with tempfile.TemporaryDirectory() as tmp, patch('llm.settings.verify_key'):
            path = Path(tmp) / 'config' / 'gemini.json'
            path.parent.mkdir(); path.write_text('{broken')
            settings = GeminiSettings(path)
            settings.save(KEY, MODEL)
            self.assertEqual(settings.read(), (KEY, MODEL))

    def test_model_access_check_uses_header_and_sanitizes_errors(self):
        response = io.BytesIO(json.dumps({'supportedGenerationMethods': ['generateContent']}).encode())
        opener = Mock(); opener.open.return_value = response
        verify_key(KEY, MODEL, opener)
        request = opener.open.call_args.args[0]
        self.assertNotIn(KEY, request.full_url)
        self.assertEqual(request.get_header('X-goog-api-key'), KEY)
        for status in (400, 401, 403, 404, 429, 503):
            opener.open.side_effect = HTTPError('https://provider', status, KEY, {}, io.BytesIO(KEY.encode()))
            with self.assertRaises(GeminiError) as error:
                verify_key(KEY, MODEL, opener)
            self.assertNotIn(KEY, str(error.exception))

    def test_saved_key_takes_precedence_over_stale_terminal_key(self):
        with tempfile.TemporaryDirectory() as tmp, patch('llm.settings.verify_key'), patch.dict(os.environ, {'GEMINI_API_KEY': 'old-environment-key'}):
            settings = GeminiSettings(Path(tmp) / 'private' / 'gemini.json')
            settings.save(KEY, MODEL)
            self.assertEqual(settings.read()[0], KEY)


class SettingsHTTPTests(unittest.TestCase):
    setUp = test_extension.HTTPTests.setUp
    tearDown = test_extension.HTTPTests.tearDown
    request = test_extension.HTTPTests.request

    def test_settings_require_auth_and_are_never_returned_to_browser(self):
        body = json.dumps({'api_key': f'export GEMINI_API_KEY="{KEY}"', 'model': MODEL})
        with patch('llm.settings.verify_key') as verify:
            self.assertEqual(self.request('POST', '/settings', {'Content-Type': 'application/json'}, body)[0], 401)
            verify.assert_not_called()
            headers = {'X-Audience-Token': 'local-test-token', 'Content-Type': 'application/json',
                       'Origin': 'chrome-extension://' + 'a' * 32}
            status, value = self.request('POST', '/settings', headers, body)
            self.assertEqual(status, 200)
            verify.assert_called_once_with(KEY, MODEL)
            self.assertNotIn(KEY, json.dumps(value))
            status, health = self.request(headers=headers)
            self.assertEqual(health['service'], 'detest')
            self.assertTrue(health['gemini_available'])
            self.assertNotIn(KEY, json.dumps(health))
            restored = AnalysisService(self.temp.name)
            self.assertEqual(restored.settings.read()[0], KEY)


if __name__ == '__main__':
    unittest.main()
