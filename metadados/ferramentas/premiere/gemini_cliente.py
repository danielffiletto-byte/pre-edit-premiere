"""Cliente REST mínimo; chave somente no header, nunca em URL/logs."""
from __future__ import annotations
import argparse
import json
import os
import random
import re
import time
from pathlib import Path
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
BASE = 'https://generativelanguage.googleapis.com/v1beta/'
MODEL = 'gemini-flash-lite-latest'


class GeminiHTTPError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def client_options():
    path = ROOT.parents[1]/'config'/'pipeline.json'
    return json.loads(path.read_text(encoding='utf-8-sig')).get('gemini', {})


def selected_model():
    value = client_options().get('model', MODEL)
    if not isinstance(value, str) or not re.fullmatch(r'gemini-[a-z0-9.-]+', value):
        raise ValueError('Nome de modelo Gemini inválido na configuração.')
    return value


def api_key():
    value = os.environ.get('GEMINI_API_KEY', '').strip()
    if not value:
        path = ROOT.parents[1] / 'config' / '.env'
        if path.is_file():
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                if line.startswith('GEMINI_API_KEY='):
                    value = line.partition('=')[2].strip().strip('\"\'')
    if not value:
        raise RuntimeError('Chave Gemini ausente: configurar GEMINI_API_KEY no .env local.')
    return value


def request(resource, payload=None, timeout=90, max_attempts=None):
    key = api_key()
    body = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(BASE + resource, data=body,
                                 headers={'x-goog-api-key': key, 'Content-Type': 'application/json'})
    attempts = client_options().get('max_attempts', 3) if max_attempts is None else max_attempts
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError('max_attempts deve ser um inteiro de 1 a 3.')
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode('utf-8')).get('error', {})
                status = detail.get('status', 'HTTP_ERROR')
                message = detail.get('message', '')
            except Exception:
                status, message = 'HTTP_ERROR', 'Resposta não reconhecida.'
            finally:
                error.close()
            message = message.replace(key, '[CHAVE OCULTA]')
            if error.code in {500, 502, 503, 504} and attempt+1 < attempts:
                delay = 2*(2**attempt) + random.uniform(0, 1)
                print(f'Análise temporariamente indisponível; nova tentativa {attempt+2}/{attempts} em {delay:.0f}s.', flush=True)
                time.sleep(delay)
                continue
            raise GeminiHTTPError(error.code, f'Gemini HTTP {error.code} / {status}: {message[:500]}') from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError('Gemini indisponível: falha de conexão ou tempo limite; nenhuma resposta confirmada.') from None


def generate(system, text, schema=None, max_tokens=8192, model=None):
    config = {'temperature': 0.2, 'maxOutputTokens': max_tokens,
              'responseMimeType': 'application/json'}
    if schema:
        config['responseJsonSchema'] = schema
    model = model or selected_model()
    options = client_options()
    fallback = options.get('fallback_model', 'gemini-flash-latest')
    if not isinstance(fallback, str) or not re.fullmatch(r'gemini-[a-z0-9.-]+', fallback):
        raise ValueError('Nome de modelo fallback Gemini inválido.')
    attempts = options.get('max_attempts', 3)
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError('max_attempts deve ser um inteiro de 1 a 3.')
    payload = {'systemInstruction': {'parts': [{'text': system}]},
                    'contents': [{'role': 'user', 'parts': [{'text': text}]}],
                    'generationConfig': config}
    primary_attempts = attempts-1 if attempts > 1 and fallback != model else attempts
    used = model
    try:
        data = request(f'models/{model}:generateContent', payload, max_attempts=primary_attempts)
    except GeminiHTTPError as error:
        if error.code not in {500, 502, 503, 504} or primary_attempts == attempts:
            raise
        print(f'Análise indisponível no modelo principal; última tentativa com {fallback}.', flush=True)
        used = fallback
        data = request(f'models/{fallback}:generateContent', payload, max_attempts=1)
    candidates = data.get('candidates', [])
    if not candidates or candidates[0].get('finishReason') not in ('STOP', None):
        raise RuntimeError('Gemini não retornou uma análise completa; plano não será aplicado.')
    result = ''.join(part.get('text', '') for part in candidates[0].get('content', {}).get('parts', [])
                     if not part.get('thought'))
    try:
        parsed = json.loads(result)
    except ValueError:
        raise RuntimeError('Gemini retornou JSON inválido; nenhuma edição aplicada.') from None
    return parsed, {**data.get('usageMetadata', {}), 'requested_model': model,
                    'model_used': used, 'model_version': data.get('modelVersion')}


def test_connection():
    model = selected_model()
    info = request(f'models/{model}')
    if 'generateContent' not in info.get('supportedGenerationMethods', []):
        raise RuntimeError('Modelo não oferece generateContent nesta conta.')
    result, usage = generate('Responda apenas JSON válido.', 'Retorne {"ok":true}.', schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}, max_tokens=512, model=model)
    if result.get('ok') is not True:
        raise RuntimeError('Autenticação respondeu, mas teste de geração não confirmou o resultado.')
    print(json.dumps({'status': 'OK', 'model': model, 'resolved_model': info.get('name'), 'usage': usage}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--testar', action='store_true')
    args = parser.parse_args()
    if args.testar:
        try:
            test_connection()
        except Exception as error:
            print(str(error))
            raise SystemExit(1)
