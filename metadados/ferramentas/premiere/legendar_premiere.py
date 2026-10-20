"""Transcrição local reutilizável -> SRT nos tempos da timeline XML vigente."""
from __future__ import annotations

import argparse
from datetime import datetime
from fractions import Fraction
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sys
import textwrap
import xml.etree.ElementTree as ET

from exportar_premiere import ROOT, DEFAULT_CLIENTS, discover, file_hash, report
from console_pipeline import LiveProgress, say, summary
from ambiente_online import model_cache

CONFIG = {'schema': 2, 'max_words': 5, 'line_chars': 32, 'max_lines': 2,
          'max_seconds': 2.5, 'gap_seconds': 0.35, 'min_seconds': 0.4}
SECTION = '## Legendas Premiere'
_MODELS = {}


def settings():
    return json.loads((ROOT.parent / 'transcricao' / 'config.json').read_text(encoding='utf-8-sig'))


def usable_transcript(data, source, options):
    if (data.get('source', {}).get('sha256') != source['sha256'] or
            data.get('settings') != options or data.get('schema_version') != 1 or
            data.get('timestamp_reference') != 'original_media_seconds'):
        return False
    segments = data.get('segments')
    if not isinstance(segments, list):
        return False
    for segment in segments:
        if segment.get('text', '').strip() and not segment.get('words'):
            return False
        for word in segment.get('words', []):
            try:
                if not (math.isfinite(float(word['start'])) and math.isfinite(float(word['end']))
                        and 0 <= float(word['start']) <= float(word['end']) <= source['duration'] + 1):
                    return False
                if not str(word['word']).strip():
                    return False
            except (ValueError, KeyError, TypeError):
                return False
    return True


def atomic_json(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def transcribe(source, options):
    os.environ.setdefault('HF_HUB_DISABLE_PROGRESS_BARS', '1')
    key = json.dumps(options, sort_keys=True)
    if key not in _MODELS:
        with LiveProgress('Carregando modelo local', inline=True):
            from faster_whisper import WhisperModel
            _MODELS[key] = WhisperModel(options['model'], device=options['device'],
                                       compute_type=options['compute_type'],
                                       download_root=str(model_cache(ROOT.parent / 'transcricao' / '.modelos')),
                                       local_files_only=True)
    segments = []
    with LiveProgress('Transcrevendo áudio localmente', inline=True) as progress:
        result, info = _MODELS[key].transcribe(
            source['path'], language=options['language'], beam_size=options['beam_size'],
            word_timestamps=True, vad_filter=False, condition_on_previous_text=False)
        for segment in result:
            segments.append({'id': len(segments), 'start': segment.start, 'end': segment.end,
                             'text': segment.text.strip(),
                             'words': [{'word': w.word, 'start': w.start, 'end': w.end,
                                        'probability': w.probability} for w in segment.words or []]})
            progress.update(segment.end / max(info.duration, .001))
    return {'schema_version': 1, 'created_at': datetime.now().astimezone().isoformat(),
            'source': {'name': source['name'], 'sha256': source['sha256']},
            'settings': options, 'timestamp_reference': 'original_media_seconds',
            'engine': {'name': 'faster-whisper', 'version': importlib.metadata.version('faster-whisper')},
            'duration_seconds': info.duration, 'segments': segments}


def get_transcript(source, metadata, options):
    options_key = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:8]
    target = metadata / f"transcricao-{source['sha256'][:12]}-{options_key}.json"
    candidates = [target]
    for folder in (metadata, Path(source['path']).parent,
                   ROOT.parent / 'transcricao', ROOT.parent / 'cortes'):
        candidates.extend(sorted(folder.glob('*transcricao*.json')))
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError):
            continue
        if usable_transcript(data, source, options):
            if candidate != target and not target.exists():
                atomic_json(target, data)
                candidate = target
            say(f"Transcrição reutilizada: {source['name']}", 'green')
            return data, candidate
    data = transcribe(source, options)
    if not usable_transcript(data, source, options):
        raise ValueError('Transcrição sem tempos por palavra válidos; SRT não gerado.')
    # Não substituir um JSON existente, inclusive edições manuais.
    if target.exists():
        index = 2
        while target.with_stem(target.stem + f'-r{index:03}').exists():
            index += 1
        target = target.with_stem(target.stem + f'-r{index:03}')
    atomic_json(target, data)
    return data, target


def map_words(source, transcript, offset):
    fps = float(Fraction(source['fps']))
    keeps, elapsed = [], offset
    for start, end in source['kept_frames']:
        keeps.append((start / fps, end / fps, elapsed))
        elapsed += (end - start) / fps
    words, warnings = [], []
    for segment in transcript['segments']:
        for word in segment.get('words', []):
            start, end = float(word['start']), float(word['end'])
            spans = [(out + max(start, a) - a, out + min(end, b) - a)
                     for a, b, out in keeps if min(end, b) > max(start, a)]
            if not spans:
                warnings.append(f"{source['name']} — {start:.3f}s: `{word['word'].strip()}` sem intervalo audível no plano; conferir no áudio.")
                continue
            words.append({'word': word['word'].strip(), 'start': min(a for a, _ in spans),
                          'end': max(b for _, b in spans)})
    return words, warnings, elapsed


def join_words(words):
    text = ' '.join(w['word'] for w in words)
    text = re.sub(r'\s+([,.;:!?%\)\]])', r'\1', text)
    text = re.sub(r'([\(\[])\s+', r'\1', text)
    return text


def wrapped(text):
    return '\n'.join(textwrap.wrap(text, CONFIG['line_chars'], break_long_words=False,
                                   break_on_hyphens=False))


def make_cues(words, duration):
    words = sorted(words, key=lambda w: (w['start'], w['end']))
    boundary = re.compile(r'[,.;:!?…]+["\u201d\u2019\u00bb\)\]\}\x27]*$')
    punctuation_only = re.compile(r'^[,.;:!?…]+["\u201d\u2019\u00bb\)\]\}\x27]*$')
    # Alguns ASRs retornam vírgula/ponto como token separado. Mantê-los na fala anterior.
    normalized = []
    for word in words:
        word = {**word, 'word': word['word'].strip()}
        if punctuation_only.fullmatch(word['word']) and normalized:
            normalized[-1]['word'] += word['word']
            normalized[-1]['end'] = max(normalized[-1]['end'], word['end'])
        else:
            normalized.append(word)
    groups, group = [], []
    for word in normalized:
        candidate = group + [word]
        if group and (len(candidate) > CONFIG['max_words'] or
                      word['end'] - group[0]['start'] > CONFIG['max_seconds'] or
                      word['start'] - group[-1]['end'] > CONFIG['gap_seconds'] or
                      len(wrapped(join_words(candidate)).splitlines()) > CONFIG['max_lines']):
            groups.append(group)
            group = []
        group.append(word)
        if boundary.search(word['word']):
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    cues = []
    for index, group in enumerate(groups):
        start = max(0, round(group[0]['start'] * 1000))
        next_start = (round(groups[index+1][0]['start'] * 1000)
                      if index + 1 < len(groups) else round(duration * 1000))
        end = min(round(duration * 1000), next_start,
                  max(round(max(w['end'] for w in group) * 1000), start + round(CONFIG['min_seconds'] * 1000)))
        text = wrapped(join_words(group))
        if end <= start:
            # Não juntar frases ou ultrapassar cinco palavras para encobrir timestamp inválido.
            raise ValueError('Tempos de legenda sobrepostos; revise os timestamps da transcrição.')
        cues.append({'start_ms': start, 'end_ms': end, 'text': text})
    return cues


def srt_clock(milliseconds):
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'


def srt_text(cues):
    previous = 0
    blocks = []
    for index, cue in enumerate(cues, 1):
        if not 0 <= previous <= cue['start_ms'] < cue['end_ms'] or not cue['text'].strip():
            raise ValueError('SRT inválido: intervalo ou texto vazio.')
        previous = cue['end_ms']
        blocks.append(f"{index}\n{srt_clock(cue['start_ms'])} --> {srt_clock(cue['end_ms'])}\n{cue['text']}\n")
    return '\n'.join(blocks)


def process(edition):
    metadata = edition / 'metadados'
    state = json.loads((metadata / 'estado-premiere.json').read_text(encoding='utf-8'))
    plan_path, xml_path = edition / state['plan'], edition / state['output']
    if file_hash(plan_path) != state['plan_sha256'] or file_hash(xml_path) != state['xml_sha256']:
        raise ValueError('Plano/XML alterado externamente. Não é seguro presumir a sincronização.')
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    options = settings()
    transcripts, transcript_paths = [], []
    for source in plan['sources']:
        if file_hash(Path(source['path'])) != source['sha256']:
            raise ValueError(f"Original alterado: {source['name']}.")
        transcript, path = get_transcript(source, metadata, options)
        transcripts.append(transcript)
        transcript_paths.append(path)
    signature = {'config': CONFIG, 'plan_sha256': state['plan_sha256'],
                 'xml_sha256': state['xml_sha256'],
                 'transcripts': [file_hash(p) for p in transcript_paths]}
    state_path = metadata / 'estado-legendas.json'
    if state_path.exists():
        previous = json.loads(state_path.read_text(encoding='utf-8'))
        output = edition / previous['output']
        if (previous['signature'] == signature and output.is_file()
                and file_hash(output) == previous['sha256']):
            say('Legendas sem alterações; SRT preservado.', 'yellow')
            return output
    with LiveProgress('Sincronizando legendas com os cortes', inline=True):
        words, warnings, offset = [], [], 0.0
        for source, transcript in zip(plan['sources'], transcripts):
            mapped, notes, offset = map_words(source, transcript, offset)
            words.extend(mapped)
            warnings.extend(notes)
        sequence = ET.parse(xml_path).getroot().find('sequence')
        xml_duration = int(sequence.findtext('duration')) / float(Fraction(plan['sources'][0]['fps']))
        if abs(xml_duration - offset) > .0001:
            raise ValueError('Plano e XML têm durações diferentes.')
        cues = make_cues(words, offset)
        if not cues:
            raise ValueError('Nenhuma legenda reconhecida; confira o áudio.')
        text = srt_text(cues)
        output = xml_path.with_name(f'legendas-{xml_path.stem}.srt')
        revision = 2
        while output.exists():
            output = xml_path.with_name(f'legendas-{xml_path.stem}-r{revision:03}.srt')
            revision += 1
        with output.open('x', encoding='utf-8-sig', newline='\n') as stream:
            stream.write(text)
        atomic_json(state_path, {'signature': signature, 'output': str(output.relative_to(edition)),
                                 'sha256': file_hash(output)})
    reading = '\n'.join(f"{srt_clock(c['start_ms']).split(',')[0]} — {c['text'].replace(chr(10), ' ')}" for c in cues)
    warning_text = '\n'.join('- ' + warning for warning in warnings) or '- Nenhum intervalo de palavra inteiramente removido.'
    report(metadata,
           f'- Status: SRT GERADO — {len(cues)} blocos; {len(words)} palavras.\n'
           f'- Sequência: `{xml_path.name}`; SRT: `../premiere/{output.name}`.\n'
           f'- Local: {options["model"]}/{options["device"]}/{options["compute_type"]}; nenhuma API utilizada.\n'
           '- Tempos recalculados pelo plano de cortes em frames, sem transcrever novamente quando há cache compatível.\n'
           '- Até 6 palavras/bloco, 32 caracteres/linha e 2 linhas, quando os tempos permitem. Sem estilo, animação ou correção automática de português.\n'
           '- Importar o SRT no Premiere e colocar na sequência em 00:00:00. Texto/fonte/tamanho editáveis no Premiere.\n'
           '- Este SRT acompanha o XML indicado, não mudanças posteriores de duração feitas no .prproj. Revisar nomes, termos técnicos e sincronismo.\n'
           f'- Avisos para revisão: {len(warnings)}.\n\n{warning_text}\n\n'
           f'#### Leitura das legendas — {xml_path.stem}\n\n{reading}',
           title='Legendas locais', section=SECTION)
    summary('Legendas prontas para importar', [f'Blocos: {len(cues)} | Avisos: {len(warnings)}',
                                             f'Resultado: {output}', 'Relatório atualizado.'])
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clientes', type=Path, default=DEFAULT_CLIENTS)
    args = parser.parse_args()
    failures = 0
    for edition, _ in discover(args.clientes.resolve()):
        say(f'\n{edition.parent.name} / {edition.name}')
        try:
            process(edition)
        except Exception as error:
            failures += 1
            say(f'ERRO: {error}', 'red')
            (edition / 'metadados').mkdir(exist_ok=True)
            report(edition / 'metadados', f'- Status: ERRO\n- Detalhe: {error}', section=SECTION)
    return int(failures > 0)


if __name__ == '__main__':
    sys.exit(main())
