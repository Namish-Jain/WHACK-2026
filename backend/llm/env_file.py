"""Load Gemini configuration from a project-local .env without shell evaluation."""
import os
import re


def load_env_file(path):
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeError):
        raise ValueError('Could not read the project .env file.') from None
    values = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
        if not match:
            raise ValueError(f'Invalid .env assignment on line {number}.')
        name, value = match.groups()
        if name not in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_MODEL'):
            continue
        if value.startswith(('"', "'")):
            quoted = re.fullmatch(r"([\"'])(.*?)\1\s*(?:#.*)?", value)
            if not quoted:
                raise ValueError(f'Invalid .env quotes on line {number}.')
            value = quoted[2]
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
        if value:
            values[name] = value
    for name, value in values.items():
        if not os.environ.get(name):
            os.environ[name] = value
