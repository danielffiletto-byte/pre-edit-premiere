"""Versoes, pausas e timestamps; dados persistidos somente no RELATORIO.md."""
from __future__ import annotations

import copy
from bisect import bisect_right
import json
from pathlib import Path
import re

VERSION_PATTERN = re.compile(r'^(.*?)(?:-corte|-recuperado)?-v(\d+)$', re.IGNORECASE)


def is_version(path):
    return VERSION_PATTERN.fullmatch(Path(path).stem) is not None


def read_history(folder):
    path = folder / 'RELATORIO.md'
    if not path.exists():
        return []
    records = {}
    for block in re.findall(r'^```json\s*\n(.*?)^```\s*$', path.read_text(encoding='utf-8-sig'), re.MULTILINE | re.DOTALL):
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or not all(key in data for key in ('output', 'source', 'config', 'removed', 'time_map')):
            continue
        if data.get('status', 'completed') != 'completed':
            continue
        output = data['output']
        if not isinstance(output, str) or Path(output).name != output:
            continue
        match = VERSION_PATTERN.fullmatch(Path(output).stem)
        if not match or not isinstance(data['source'], dict):
            continue
        data['version'] = int(match.group(2))
        records[output.casefold()] = data
    return sorted(records.values(), key=lambda record: record['version'])


def next_output(folder, source):
    used = [0]
    for path in folder.iterdir():
        match = re.fullmatch(re.escape(source.stem) + r'(?:-corte|-recuperado)?-v(\d+)', path.stem, re.IGNORECASE)
        if match:
            used.append(int(match.group(1)))
    for record in read_history(folder):
        if record['source'].get('name', '').casefold() == source.name.casefold():
            used.append(record['version'])
    number = max(used) + 1
    return folder / f'{source.stem}-v{number:03}.mp4', number


def timeline(cuts, duration, speed=1):
    if duration <= 0 or not 0.5 <= speed <= 2:
        raise ValueError('Duracao ou velocidade invalida no plano.')
    cursor = edited = 0.0
    keeps = []
    for cut in sorted(cuts, key=lambda item: item['start']) + [{'start': duration, 'end': duration}]:
        start, end = float(cut['start']), float(cut['end'])
        if start < cursor - 0.000001 or end < start or end > duration + 0.001:
            raise ValueError('Plano de cortes com intervalos invalidos ou sobrepostos.')
        if start > cursor + 0.000001:
            length = start - cursor
            keeps.append({'source_start': cursor, 'source_end': start,
                          'output_start': edited / speed, 'output_end': (edited + length) / speed,
                          'playback_speed': speed})
            edited += length
        cursor = end
    return keeps, edited / speed


def map_time(value, cuts, speed):
    removed = sum(max(0, min(value, cut['end']) - cut['start']) for cut in cuts)
    return max(0, (value - removed) / speed)


def timestamp(value):
    milliseconds = max(0, round(value * 1000))
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}.{ms:03}'


def load_transcript(folder, source, fingerprint):
    path = folder / f'{source.stem}.transcricao.json'
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    expected = data.get('source', {}).get('sha256')
    if expected and expected != fingerprint:
        raise ValueError('O JSON de transcricao nao corresponde ao video. Confira os arquivos.')
    return data


def catalog(cuts, transcript=None):
    words = sorted([word for segment in (transcript or {}).get('segments', [])
                    for word in segment.get('words', [])], key=lambda word: word['start'])
    result = copy.deepcopy(cuts)
    for index, cut in enumerate(result, 1):
        cut.setdefault('id', index)
        if words:
            before = [word['word'].strip() for word in words if word['end'] <= cut['start'] + 0.03][-8:]
            after = [word['word'].strip() for word in words if word['start'] >= cut['end'] - 0.03][:8]
            cut['before'] = ' '.join(before) or '[início do vídeo]'
            cut['after'] = ' '.join(after) or '[fim do vídeo]'
        else:
            cut.setdefault('before', '[contexto indisponível]')
            cut.setdefault('after', '[contexto indisponível]')
    ids = [cut['id'] for cut in result]
    if any(not isinstance(value, int) or value < 1 for value in ids) or len(ids) != len(set(ids)):
        raise ValueError('IDs de cortes invalidos ou duplicados no historico.')
    return result


def prepare_record(record, transcript=None):
    """Le tambem os blocos tecnicos das versoes anteriores do cortador."""
    data = copy.deepcopy(record)
    data['all_cuts'] = catalog(data.get('all_cuts', data['removed']), transcript)
    by_bounds = {(round(c['start'], 8), round(c['end'], 8)): c for c in data['all_cuts']}
    active = []
    for cut in data['removed']:
        full = by_bounds.get((round(cut['start'], 8), round(cut['end'], 8)))
        if not full:
            raise ValueError('Historico inconsistente: corte nao encontrado no catalogo.')
        active.append(copy.deepcopy(full))
    data['removed'] = active
    data.setdefault('restored_pause_ids', [])
    return data


def pause_rows(record):
    speed = float(record['config'].get('playback_speed', 1))
    rows = []
    for cut in record['removed']:
        point = map_time(cut['start'], record['removed'], speed)
        original = cut.get('detected_end', cut['end']) - cut.get('detected_start', cut['start'])
        remaining = (original - (cut['end']-cut['start'])) / speed
        rows.append({**cut, 'output_time': point, 'original_pause': original,
                     'remaining_pause': max(0, remaining)})
    return rows


def reading_blocks(record, transcript=None):
    """Percorre a fala uma vez e insere cada marcador na fronteira de palavras mais proxima."""
    words = []
    for segment in (transcript or {}).get('segments', []):
        items = segment.get('words') or [{'word': segment.get('text', ''),
                                         'start': segment['start'], 'end': segment['end']}]
        for item in items:
            parts = item['word'].strip().split()
            for index, text in enumerate(parts):
                length = max(0, item['end']-item['start'])
                words.append({'text': text, 'start': item['start'] + length*index/len(parts),
                              'end': item['start'] + length*(index+1)/len(parts)})
    words.sort(key=lambda word: word['start'])
    rows = sorted(pause_rows(record), key=lambda row: row['start'])
    if not words:
        # Sem a transcricao, nao tentar reconstruir uma fala juntando contextos sobrepostos.
        return [{'text': f'[CORTE {row["id"]}]', 'cuts': [row]} for row in rows]
    midpoints = []
    for word in words:
        midpoints.append(max(midpoints[-1] if midpoints else 0, (word['start']+word['end'])/2))
    at = {}
    for row in rows:
        point = (row['start']+row['end'])/2
        position = bisect_right(midpoints, point)
        at.setdefault(position, []).append(row)
    blocks, tokens, markers = [], [], []
    word_count = 0
    for position in range(len(words)+1):
        for row in at.get(position, []):
            tokens.append(f'[CORTE {row["id"]}]')
            markers.append(row)
        sentence_end = position > 0 and re.search(r'[.!?…][\"\u201d\u2019\)\]]*$', words[position-1]['text'])
        if tokens and (sentence_end or word_count >= 32 or position == len(words)):
            blocks.append({'text': ' '.join(tokens), 'cuts': markers})
            tokens, markers, word_count = [], [], 0
        if position < len(words):
            tokens.append(words[position]['text'])
            word_count += 1
    return blocks


def reading_line(block):
    return block['text']


def continuous_reading(record, transcript=None):
    return ' '.join(reading_line(block) for block in reading_blocks(record, transcript))


def break_after_cuts(text):
    # Um grupo de marcadores consecutivos fica na mesma linha; quebrar somente no ultimo.
    return re.sub(r'(\[CORTE \d+\](?:\s+\[CORTE \d+\])*)\s*',
                  lambda match: match.group(1) + '\n', text).rstrip()


def cut_reading(record, transcript=None):
    return break_after_cuts(continuous_reading(record, transcript))


def pause_list(record, transcript=None):
    lines = [f'Pontos de corte em **{record["output"]}**:', '']
    if record['removed']:
        if not transcript:
            lines += ['Transcrição indisponível; exibindo somente os marcadores.', '']
        lines.append(cut_reading(record, transcript))
    else:
        lines.append('Nenhuma pausa encurtada nesta versão.')
    return '\n'.join(lines)


def update_pause_list(folder, record, transcript):
    """Atualiza somente a leitura humana da versao; preserva JSON e demais historicos."""
    path = folder / 'RELATORIO.md'
    text = path.read_text(encoding='utf-8-sig')
    pattern = re.compile(r'^Pontos de corte em \*\*' + re.escape(record['output'])
                         + r'\*\*[^\n]*\n.*?(?=^<details>|^## |^### |\Z)', re.MULTILINE | re.DOTALL)
    updated, count = pattern.subn(lambda match: pause_list(record, transcript).rstrip() + '\n\n', text)
    if count and updated != text:
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(updated, encoding='utf-8')
        temporary.replace(path)
    return count > 0


def transcript_markdown(record, transcript):
    if not transcript:
        return 'Transcrição original não encontrada na pasta. Os cortes continuam localizáveis pelos timestamps.'
    speed = float(record['config'].get('playback_speed', 1))
    cuts = record['removed']
    lines = ['Tempos remapeados da transcrição original; não substituem o alinhamento final das legendas.', '']
    for segment in transcript.get('segments', []):
        start = map_time(segment['start'], cuts, speed)
        end = map_time(segment['end'], cuts, speed)
        if end-start > 0.00001:
            lines.append(f'- **{timestamp(start)} → {timestamp(end)}** | {segment["text"].strip()}')
    return '\n'.join(lines)


def record_details(record, transcript):
    technical = json.dumps(record, ensure_ascii=False, indent=2)
    return (pause_list(record, transcript) + '\n\n<details>\n<summary>Transcrição com tempos desta versão</summary>\n\n'
            + transcript_markdown(record, transcript) + '\n\n</details>\n\n'
            '<details>\n<summary>Dados técnicos dos cortes e mapa temporal</summary>\n\n'
            f'```json\n{technical}\n```\n\n</details>')


def parse_ids(text):
    text = re.sub(r'\be\b', ' ', text.strip(), flags=re.IGNORECASE)
    if not text.strip():
        return set()
    if not re.fullmatch(r'[\d\s,;]+', text):
        raise ValueError('Use numeros separados por espaco, virgula ou "e".')
    values = {int(value) for value in re.findall(r'\d+', text)}
    if not values or min(values) < 1:
        raise ValueError('Os numeros dos cortes comecam em 1.')
    return values


def select_pauses(record):
    allowed = {cut['id'] for cut in record['removed']}
    print('Quais pausas gostaria de recuperar?')
    print('Ex.: 1, 5 e 7. Pode enviar uma por linha; ENTER vazio finaliza.')
    selected = set()
    while True:
        value = input('> ')
        if not value.strip():
            return selected
        try:
            values = parse_ids(value)
            if values - allowed:
                raise ValueError('Cortes indisponiveis nesta versao: ' + ', '.join(map(str, sorted(values-allowed))))
            selected |= values
        except ValueError as error:
            print(str(error))
