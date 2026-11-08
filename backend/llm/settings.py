"""Server-side Gemini settings. Secrets never enter Chrome storage or reports."""
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from llm.explain import DEFAULT_MODEL, GeminiError, NoRedirect


def normalize_api_key(value):
    """Accept a pasted key, quoted value or environment assignment; never log it."""
    if not isinstance(value, str):
        raise ValueError('Paste your Gemini API key into the key field.')
    value = ''.join(c for c in value if unicodedata.category(c) != 'Cf').strip()
    if value.startswith('```') and value.endswith('```'):
        value = value[3:-3].strip()
        first, separator, rest = value.partition('\n')
        if separator and first.lower() in ('bash', 'sh', 'zsh', 'env', 'text'):
            value = rest.strip()
    assignment = re.fullmatch(r'(?:export\s+)?(?:GEMINI_API_KEY|GOOGLE_API_KEY)\s*=\s*(.*)', value, re.S)
    if assignment:
        value = assignment[1].strip()
    pairs = {'"': '"', "'": "'", '`': '`', '“': '”', '‘': '’'}
    if len(value) >= 2 and pairs.get(value[0]) == value[-1]:
        value = value[1:-1]
    value = ''.join(value.split())
    if value and not re.fullmatch(r'[A-Za-z0-9_+=/.-]{1,512}', value):
        raise ValueError('Paste only the API key or a GEMINI_API_KEY assignment, without surrounding instructions.')
    return value


def provider_error(status):
    return {
        400: 'Gemini rejected the request. Check your API key and model in Connection.',
        401: 'Gemini rejected the API key. Update it in Connection.',
        403: 'This API key does not have access. Check its restrictions in Google AI Studio.',
        404: 'This Gemini model is unavailable. Choose another model in Connection.',
        429: 'Gemini quota or rate limit reached. Check your Google AI Studio quota, then retry.',
        500: 'Gemini is temporarily unavailable. Retry in a moment.',
        503: 'Gemini is temporarily unavailable. Retry in a moment.',
    }.get(status, f'Gemini request failed (HTTP {status}). Please retry.')


def verify_key(api_key, model, opener=None):
    """Check key/model access without spending a generation request."""
    request = Request(f'https://generativelanguage.googleapis.com/v1beta/models/{model}',
                      headers={'x-goog-api-key': api_key, 'Accept': 'application/json'})
    try:
        with (opener or build_opener(NoRedirect())).open(request, timeout=15) as response:
            data = json.load(response)
        if not isinstance(data, dict) or 'generateContent' not in data.get('supportedGenerationMethods', []):
            raise GeminiError('Choose a Gemini model that supports text generation.')
    except HTTPError as exc:
        exc.close()
        raise GeminiError(provider_error(exc.code)) from None
    except (URLError, OSError, ValueError, TypeError):
        raise GeminiError('Could not connect to Gemini. Check your connection and try again.') from None


class GeminiSettings:
    def __init__(self, path, model=None):
        self.path = Path(path)
        self.model_override = model

    def read(self):
        saved = {}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                if not isinstance(saved, dict):
                    raise ValueError()
            except (OSError, ValueError):
                raise GeminiError('Saved Gemini settings could not be read. Save your key again in Connection.') from None
        key = saved.get('api_key') or os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY') or ''
        model = self.model_override or saved.get('model') or os.getenv('GEMINI_MODEL') or DEFAULT_MODEL
        if not isinstance(key, str) or not isinstance(model, str):
            raise GeminiError('Invalid Gemini settings. Save your key again in Connection.')
        return key.strip(), model.strip()

    def public(self):
        key, model = self.read()
        return {'gemini_available': bool(key), 'model': model}

    def save(self, api_key, model=None):
        if not isinstance(api_key, str) or (model is not None and not isinstance(model, str)):
            raise ValueError('Enter a valid API key and model.')
        try:
            current_key, current_model = self.read()
        except GeminiError:
            if not api_key.strip():
                raise
            current_key, current_model = '', self.model_override or os.getenv('GEMINI_MODEL') or DEFAULT_MODEL
        entered_key = normalize_api_key(api_key)
        if api_key.strip() and not entered_key:
            raise ValueError('The pasted value is empty. Paste the API key itself.')
        key = entered_key or normalize_api_key(current_key)
        model = self.model_override or (model or current_model).strip()
        if not key:
            raise ValueError('The Gemini key field is empty. Paste your key, then select Save key.')
        if not re.fullmatch(r'gemini-[A-Za-z0-9._-]+', model):
            raise ValueError('Enter a valid Gemini model name.')
        verify_key(key, model)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.gemini-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump({'api_key': key, 'model': model}, stream)
                stream.write('\n')
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.public()
