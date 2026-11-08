"""Optional one-sentence Gemini explanation; Python always owns the rating."""
import json
import os
import re
import socket
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from llm.explain import DEFAULT_MODEL, GeminiError, NoRedirect, prepare_evidence
from history import atomic_json
from llm.settings import provider_error

PROMPT = Path(__file__).with_name('brief_prompt.txt')


def validate_reason(value):
    if not isinstance(value, dict) or set(value) != {'reason'} or not isinstance(value['reason'], str):
        raise GeminiError('Gemini did not return the required short explanation.')
    reason = value['reason'].strip()
    if (not 5 <= len(reason.split()) <= 40 or len(reason) > 450 or
            not reason.endswith('.') or reason.count('.') != 1 or
            re.search(r'[\d{}<>!?\n]|https?://|\]\(', reason) or
            re.search(r'\b(fake|genuine|fraud\w*|trustworthy|bots?|purchased|authentic|proves?|probability|significan\w*)\b', reason, re.I)):
        raise GeminiError('Gemini wording failed the short-explanation checks.')
    return reason


def generate_brief(report, out, model=None, opener=None, api_key=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    key = (api_key or os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY') or '').strip()
    model = model or os.getenv('GEMINI_MODEL') or DEFAULT_MODEL
    if not key:
        raise GeminiError('Add your Gemini API key in Connection.')
    if not re.fullmatch(r'gemini-[A-Za-z0-9._-]+', model):
        raise GeminiError('Invalid Gemini model name.')
    evidence = prepare_evidence(report)
    body = {'systemInstruction': {'parts': [{'text': PROMPT.read_text()}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(evidence, allow_nan=False)}]}],
            'generationConfig': {'responseMimeType': 'application/json', 'maxOutputTokens': 4096,
                                 'responseSchema': {'type': 'OBJECT', 'properties': {'reason': {'type': 'STRING'}}, 'required': ['reason']}}}
    if model == 'gemini-3.8-flash':
        body['generationConfig']['thinkingConfig'] = {'thinkingLevel': 'low'}
    atomic_json(out / 'input.json', evidence)
    atomic_json(out / 'request.json', body)
    (out / 'prompt.txt').write_text(PROMPT.read_text())
    request = Request(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                      data=json.dumps(body).encode(), method='POST',
                      headers={'Content-Type': 'application/json', 'x-goog-api-key': key})
    try:
        with (opener or build_opener(NoRedirect())).open(request, timeout=120) as response:
            raw = json.load(response)
        candidates = raw.get('candidates', [])
        if not candidates or candidates[0].get('finishReason') != 'STOP':
            raise GeminiError('Gemini did not complete its explanation.')
        parts = candidates[0].get('content', {}).get('parts', [])
        text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
        reason = validate_reason(json.loads(text))
    except HTTPError as exc:
        exc.close()
        raise GeminiError(provider_error(exc.code)) from None
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            message = 'Python could not verify Gemini’s TLS certificate. Check your Python certificate installation.'
        elif isinstance(exc.reason, (TimeoutError, socket.timeout)):
            message = 'Gemini took too long to respond. Your statistics are saved; retry the explanation.'
        elif isinstance(exc.reason, socket.gaierror):
            message = 'Could not find the Gemini server. Check your internet connection and DNS, then retry.'
        else:
            message = 'Could not connect to Gemini. Check your internet connection, then retry.'
        raise GeminiError(message) from None
    except TimeoutError:
        raise GeminiError('Gemini took too long to respond. Your statistics are saved; retry the explanation.') from None
    except OSError:
        raise GeminiError('The connection to Gemini was interrupted. Retry the explanation.') from None
    except (ValueError, TypeError, AttributeError):
        raise GeminiError('Gemini returned an unreadable response. Retry the explanation.') from None
    atomic_json(out / 'explanation.json', {'reason': reason, 'requested_model': model,
                                         'returned_model': raw.get('modelVersion')})
    return reason
