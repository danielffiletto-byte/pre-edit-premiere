"""Pré-edição piloto: originais -> análise local -> timeline XMEML 5.

Não renderiza vídeo, não altera fontes e não requer transcrição nesta etapa.
Referência: Apple, Final Cut Pro 7 XML Interchange Format / Basics of Encoding.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from fractions import Fraction
import io
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'cortes'))
from cortar_silencios import (ffmpeg_run, file_hash, make_plan, media_info,
                             parse_silences, report)
from console_pipeline import LiveProgress, say, summary
from geometria_video import CANVAS, normalized_rotation, display_size
from normalizacao_audio import limited_gain_db

CENTRAL = ROOT.parents[1]
DEFAULT_CLIENTS = Path(os.environ.get('PREEDITOR_CLIENTES', str(CENTRAL.parent)))
CONFIG = {'schema': 2, 'silence_threshold_dbfs': -25.0,
          'pause_seconds': 0.2, 'normalize_peak_dbfs': -1.0,
          'speed': 1.0, 'stage': 'xml_treated_audio',
          'audio': {'noise_reduction_db': 3.0, 'sample_rate': 48000,
                    'pcm_depth': 24, 'peak_target_dbfs': 0.0, 'max_gain_db': 6.0}}
DETECTION_KEYS = ('silence_threshold_dbfs', 'pause_seconds', 'normalize_peak_dbfs', 'speed')
EXTENSIONS = {'.mp4', '.mov'}
SECTION = '## Pré-edição Premiere'


class VideoTimingError(ValueError):
    """Tempos de apresentação inválidos; nunca ignorar para montar os cortes."""


def presentation_signature(pts, time_base):
    """Resumo exato dos intervalos, independente da unidade usada no contêiner."""
    base = Fraction(time_base)
    digest = hashlib.sha256()
    first = pts[0]
    for value in pts:
        offset = (value-first)*base
        digest.update(f'{offset.numerator}/{offset.denominator}\n'.encode('ascii'))
    return digest.hexdigest()


def natural_key(path):
    return tuple((1, int(s)) if s.isdigit() else (0, s.casefold())
                 for s in re.split(r'(\d+)', path.name))


def premiere_url(path):
    """URL local no estilo exportado pelo Premiere, inclusive letra de unidade."""
    windows = PureWindowsPath(path)
    if windows.drive.startswith('\\\\'):
        raise ValueError('Copie as mídias para um disco local antes de gerar o XML.')
    if windows.drive:
        if not windows.is_absolute():
            raise ValueError('O caminho da mídia deve ser absoluto.')
        return 'file://localhost/' + quote(windows.as_posix(), safe='/').replace('%3A', '%3a')
    return Path(path).resolve().as_uri().replace('file:///', 'file://localhost/', 1)


def local_url_path(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'file' or parsed.netloc not in ('localhost', ''):
        raise ValueError('URL de mídia não local.')
    path = unquote(parsed.path)
    if re.match(r'^/[A-Za-z]:/', path):
        path = path[1:]
    return Path(path)


def reusable_analysis(previous, edition, sources):
    """Reusa somente planos íntegros da mesma fonte e da mesma regra de cortes."""
    if not previous:
        return None
    path = edition / previous['plan']
    if not path.is_file() or file_hash(path) != previous.get('plan_sha256'):
        return None
    record = json.loads(path.read_text(encoding='utf-8'))
    if any(record['config'].get(k) != CONFIG[k] for k in DETECTION_KEYS):
        return None
    plans = record['sources']
    if len(plans) != len(sources):
        return None
    if any(any(plan.get(k) != value for k, value in source.items())
           for plan, source in zip(plans, sources)):
        return None
    return [{k: v for k, v in plan.items() if k != 'treated_audio'} for plan in plans]


def write_treated_audio(source, destination, debug, audio_options=None):
    """WAV completo: mantém handles, canais e pitch; nenhuma aceleração."""
    audio = audio_options or CONFIG['audio']
    filters = (f"aresample={audio['sample_rate']}:async=1:first_pts=0,"
               f"afftdn=nr={audio['noise_reduction_db']}:nf=-50:tn=1:gs=6,"
               f"apad=whole_dur={source['duration']:.9f},atrim=end={source['duration']:.9f}")
    # Medir após denoise evita compensar pelo pico de um ruído já removido.
    measured = ffmpeg_run(['-i', source['path'], '-map', '0:a:0', '-vn', '-af',
                           filters + ',astats=measure_perchannel=none:measure_overall=Peak_level',
                           '-f', 'null', '-'], '2/4 Medindo voz tratada', source['duration'], debug)
    match = re.search(r'Peak level dB:\s*(-?[\d.]+)', measured)
    if not match:
        raise ValueError('Não foi possível medir o pico do áudio tratado.')
    if float(match.group(1)) <= -80:
        raise ValueError('Áudio tratado praticamente mudo; processamento interrompido.')
    # Uma fração de LSB abaixo do teto evita clipping por arredondamento em 0 dBFS.
    gain = limited_gain_db(float(match.group(1)),
                           target_dbfs=min(audio['peak_target_dbfs'], -0.000002),
                           max_gain_db=audio.get('max_gain_db', 6.0))
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_run(['-i', source['path'], '-map', '0:a:0', '-vn', '-af',
                filters + f',volume={gain:.6f}dB', '-ar', str(audio['sample_rate']),
                '-c:a', 'pcm_s24le', '-n', str(destination)],
               '3/4 Preparando áudio tratado', source['duration'], debug)
    info = media_info(destination)
    stream = next(s for s in info['streams'] if s['codec_type'] == 'audio')
    if (stream['channels'] != source['channels'] or
            abs(float(stream['duration']) - source['duration']) > 1 / audio['sample_rate']):
        raise ValueError('Áudio tratado com canais ou duração inesperados.')
    return {'path': str(destination.resolve()), 'name': destination.name,
            'sha256': file_hash(destination), 'fps': source['fps'],
            'frames': source['frames'], 'duration': source['duration'],
            'channels': source['channels'], 'sample_rate': audio['sample_rate'],
            'pcm_depth': audio['pcm_depth'], 'gain_db': gain,
            'max_gain_db': min(6.0, audio.get('max_gain_db', 6.0)),
            'audio_only': True}


def discover(clients):
    from converter_hdr import generated_sdr
    for client in sorted(clients.iterdir(), key=natural_key):
        if (not client.is_dir() or client.is_symlink() or client.is_junction() or
                client.name.casefold() in {'metadados', 'modelo premiere', 'modelo-premiere'}):
            continue
        for edition in sorted(client.iterdir(), key=natural_key):
            if not edition.is_dir() or edition.is_symlink() or edition.is_junction():
                continue
            videos = sorted((p for p in edition.iterdir() if p.is_file()
                             and not p.is_symlink() and not generated_sdr(p)
                             and p.suffix.lower() in EXTENSIONS),
                            key=natural_key)
            if videos:
                keys = [natural_key(p) for p in videos]
                if len(set(keys)) != len(keys):
                    raise ValueError(f'Ordem ambígua em {edition}; use prefixos únicos.')
                yield edition, videos


def rate_fields(fps):
    if fps.denominator == 1 and 1 <= fps <= 120:
        return int(fps), False
    if fps in {Fraction(n * 1000, 1001) for n in (24, 30, 60, 120)}:
        return round(float(fps)), True
    raise ValueError(f'FPS {fps} ainda não suportado pelo piloto.')


def verified_cadence(pts, time_base, nominal_rate, average_rate):
    """Escolhe uma cadência padrão somente após conferir todos os timestamps.

    MOV pode declarar média fracionária por pequenas variações dos timestamps.
    Aceita desvio absoluto de até 1/4 de frame no arquivo inteiro; não arredonda
    VFR livremente, não reencoda e não muda os timestamps da mídia original.
    """
    if not pts or any(b <= a for a, b in zip(pts, pts[1:])):
        raise ValueError('Timestamps de vídeo ausentes ou duplicados; revise a cadência.')
    rates = []
    for value in (nominal_rate, average_rate):
        try:
            candidate = Fraction(value)
            rate_fields(candidate)
        except (ValueError, ZeroDivisionError, TypeError):
            continue
        if candidate not in rates:
            rates.append(candidate)
    if not rates:
        raise ValueError(f'Cadência nominal/média sem taxa padrão suportada: {nominal_rate} / {average_rate}.')
    base = Fraction(time_base)
    if base <= 0:
        raise ValueError('Base de tempo do vídeo inválida.')
    for candidate in rates:
        maximum = max(abs((p-pts[0])*base*candidate-i) for i, p in enumerate(pts))
        if maximum <= Fraction(1, 4):
            return candidate, {'nominal_fps': str(nominal_rate), 'average_fps': str(average_rate),
                               'selected_fps': str(candidate),
                               'max_deviation_frames': float(maximum),
                               'max_deviation_ms': float(maximum/candidate*1000),
                               'tolerance_frames': .25, 'video_reencoded': False}
    raise ValueError('Cadência variável/irregular: timestamps desviam mais de 1/4 de frame da taxa nominal. Nenhuma conversão automática foi feita.')


def probe(path):
    info = media_info(path)
    video = [s for s in info['streams'] if s['codec_type'] == 'video'
             and not s.get('disposition', {}).get('attached_pic')]
    audio = [s for s in info['streams'] if s['codec_type'] == 'audio']
    if len(video) != 1 or len(audio) != 1 or audio[0].get('channels') not in (1, 2):
        raise ValueError(f'{path.name}: o piloto exige um vídeo e um áudio mono/estéreo.')
    v, a = video[0], audio[0]
    if v.get('field_order', 'progressive') not in ('progressive', 'unknown'):
        raise ValueError('Vídeo entrelaçado exige validação específica.')
    if v.get('sample_aspect_ratio', '1:1') not in ('1:1', 'N/A'):
        raise ValueError('Pixel não quadrado exige validação específica.')
    rotation = float(v.get('tags', {}).get('rotate', 0))
    for side in v.get('side_data_list', []):
        rotation = float(side.get('rotation', rotation))
    if abs(float(v.get('start_time', 0)) - float(a.get('start_time', 0))) > 0.001:
        raise ValueError('Inícios de áudio/vídeo distintos exigem validação específica.')
    # Verifica a cadência real pelos PTS, sem decodificar ou reencodar o vídeo.
    result = subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-select_streams',
                             'v:0', '-show_packets', '-show_entries', 'packet=pts',
                             '-of', 'json', str(path)], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=120)
    if result.returncode:
        raise ValueError('Não foi possível verificar a cadência do vídeo.')
    try:
        pts = sorted(int(packet['pts']) for packet in json.loads(result.stdout)['packets'])
    except (KeyError, ValueError):
        raise VideoTimingError(f'{path.name}: quadro sem marcação de tempo válida; original preservado.') from None
    timing = media_timing(pts, v['time_base'], v.get('r_frame_rate'),
                          v.get('avg_frame_rate'), v.get('duration'))
    fps, cadence = Fraction(timing['fps']), timing['cadence']
    rotation = normalized_rotation(rotation)
    frames, duration = timing['frames'], timing['duration']
    if float(a.get('duration', duration)) + 1 / float(fps) < duration:
        raise ValueError('Áudio mais curto que o vídeo exige validação específica.')
    return {'path': str(path.resolve()), 'name': path.name, 'sha256': file_hash(path),
            'pixel_format': v.get('pix_fmt', ''),
            'fps': str(fps), 'frames': frames, 'duration': duration,
            'media_fps': timing['media_fps'], 'decoded_frames': len(pts),
            'video_time_base': str(Fraction(v['time_base'])),
            'presentation_sha256': presentation_signature(pts, v['time_base']),
            'width': v['width'], 'height': v['height'], 'channels': a['channels'],
            'rotation_degrees': rotation,
            'sample_rate': int(a['sample_rate']),
            'cadence': cadence,
            'color': {k: v.get(k, 'não informado') for k in
                      ('color_space', 'color_transfer', 'color_primaries')}}


def media_timing(pts, time_base, nominal_rate, average_rate, duration):
    """A grade da timeline não é a contagem de imagens de uma mídia VFR.

    Mantém o tempo real do arquivo e usa frames apenas como grade de edição.
    O importador do Premiere continua responsável por ler os PTS originais.
    Não usa Interpret Footage/Assume Frame Rate nem muda velocidade.
    """
    if not pts or any(b <= a for a, b in zip(pts, pts[1:])):
        raise VideoTimingError('Tempos dos quadros ausentes ou repetidos; não é seguro posicionar os cortes.')
    base = Fraction(time_base)
    actual_duration = Fraction(str(duration))
    if base <= 0 or actual_duration <= 0 or (pts[-1]-pts[0])*base >= actual_duration:
        raise VideoTimingError('Duração e tempos dos quadros inconsistentes; original preservado.')
    rates = []
    for value in (nominal_rate, average_rate):
        try:
            rate = Fraction(value)
            rate_fields(rate)
        except (ValueError, TypeError, ZeroDivisionError):
            continue
        if rate not in rates:
            rates.append(rate)
    if not rates:
        raise ValueError(f'Sem taxa padrão para a timeline: {nominal_rate} / {average_rate}.')
    try:
        fps, cadence = verified_cadence(pts, time_base, nominal_rate, average_rate)
        cadence['mode'] = 'regular'
    except ValueError:
        fps = rates[0]
        maximum = max(abs((p-pts[0])*base*fps-i) for i, p in enumerate(pts))
        cadence = {'mode': 'vfr_original', 'nominal_fps': str(nominal_rate),
                   'average_fps': str(average_rate), 'selected_fps': str(fps),
                   'max_deviation_frames': float(maximum),
                   'max_deviation_ms': float(maximum/fps*1000),
                   'video_reencoded': False,
                   'handling': 'Tempo real preservado; cortes na grade da sequência, leitura VFR pelo Premiere'}
    try:
        media_fps = Fraction(average_rate)
        if media_fps <= 0:
            raise ValueError('FPS médio inválido.')
    except (ValueError, TypeError, ZeroDivisionError):
        media_fps = Fraction(len(pts), 1)/actual_duration
    frames = int(actual_duration*fps + Fraction(1, 1000000))
    if frames < 1:
        raise ValueError('Vídeo menor que um frame da timeline.')
    return {'fps': str(fps), 'media_fps': str(media_fps), 'frames': frames,
            'duration': float(actual_duration), 'cadence': cadence}


def timeline_grid(source, fps):
    """Uma grade comum por edição, sem reinterpretar a velocidade das fontes."""
    fps = Fraction(fps)
    rate_fields(fps)
    frames = int(Fraction(str(source['duration']))*fps + Fraction(1, 1000000))
    if frames < 1:
        raise ValueError('Vídeo menor que um frame da sequência.')
    return {**source, 'fps': str(fps), 'frames': frames,
            'cadence': {**source['cadence'], 'timeline_fps': str(fps),
                        'tail_outside_grid_ms': max(0.0, (source['duration']-frames/float(fps))*1000)}}


def quantize(cuts, frames, fps):
    """Encolhe a remoção para dentro do silêncio, sem invadir fala nos limites."""
    removed = []
    for cut in cuts:
        start = max(0, math.ceil(cut['start'] * fps - 1e-8))
        end = min(frames, math.floor(cut['end'] * fps + 1e-8))
        if end > start:
            if removed and start <= removed[-1][1]:
                removed[-1] = (removed[-1][0], max(end, removed[-1][1]))
            else:
                removed.append((start, end))
    keeps, cursor = [], 0
    for start, end in removed + [(frames, frames)]:
        if start > cursor:
            keeps.append((cursor, start))
        cursor = end
    if not keeps:
        raise ValueError('Não restou conteúdo após a análise.')
    return removed, keeps


def analyze(source, debug):
    path, duration = source['path'], source['duration']
    measured = ffmpeg_run(['-i', path, '-map', '0:a:0', '-vn', '-af', 'volumedetect',
                           '-f', 'null', '-'], '2/4 Medindo áudio', duration, debug)
    match = re.search(r'max_volume:\s*(-?[\d.]+) dB', measured)
    if not match or float(match.group(1)) <= -80:
        raise ValueError('Áudio ausente ou praticamente mudo; revise o original.')
    gain = CONFIG['normalize_peak_dbfs'] - float(match.group(1))
    # Normalização somente para análise; o áudio da timeline permanece original.
    detected = ffmpeg_run(['-i', path, '-map', '0:a:0', '-vn', '-af',
                           f"volume={gain:.6f}dB,silencedetect=noise={CONFIG['silence_threshold_dbfs']}dB:d={CONFIG['pause_seconds']}",
                           '-f', 'null', '-'], '3/4 Identificando pausas', duration, debug)
    silences = parse_silences(detected, duration)
    if sum(e - s for s, e in silences) >= duration - 0.01:
        raise ValueError('Todo o áudio abaixo do limiar; revise a gravação.')
    cuts, _, _ = make_plan(silences, duration, CONFIG['pause_seconds'])
    removed, keeps = quantize(cuts, source['frames'], float(Fraction(source['fps'])))
    return {**source, 'analysis_gain_db': gain,
            'removed_frames': removed, 'kept_frames': keeps}


def node(parent, name, text=None, **attrs):
    item = ET.SubElement(parent, name, attrs)
    if text is not None:
        item.text = str(text)
    return item


def rate(parent, fps):
    timebase, ntsc = rate_fields(Fraction(fps))
    item = node(parent, 'rate')
    node(item, 'timebase', timebase)
    node(item, 'ntsc', 'TRUE' if ntsc else 'FALSE')


def video_format(parent, source):
    sc = node(parent, 'samplecharacteristics')
    rate(sc, source['fps'])
    width, height = display_size(source)
    node(sc, 'width', width)
    node(sc, 'height', height)
    node(sc, 'anamorphic', 'FALSE')
    node(sc, 'pixelaspectratio', 'square')
    node(sc, 'fielddominance', 'none')


def source_file(parent, source, file_id, full):
    f = node(parent, 'file', id=file_id)
    if not full:
        return
    node(f, 'name', source['name'])
    node(f, 'pathurl', premiere_url(source['path']))
    rate(f, source['fps'])
    node(f, 'duration', source['frames'])
    media = node(f, 'media')
    if not source.get('audio_only'):
        video_format(node(media, 'video'), source)
    audio = node(media, 'audio')
    sc = node(audio, 'samplecharacteristics')
    node(sc, 'depth', source.get('pcm_depth', 16))
    node(sc, 'samplerate', source['sample_rate'])
    node(audio, 'channelcount', source['channels'])


def build_xml(name, sources, sequence_size=None):
    treated = all('treated_audio' in s for s in sources)
    if any('treated_audio' in s for s in sources) and not treated:
        raise ValueError('Todas as fontes devem ter áudio tratado, ou nenhuma.')
    root = ET.Element('xmeml', version='5')
    sequence = node(root, 'sequence', id='sequence-1')
    node(sequence, 'name', name)
    total = sum(end - start for s in sources for start, end in s['kept_frames'])
    node(sequence, 'duration', total)
    rate(sequence, sources[0]['fps'])
    tc = node(sequence, 'timecode')
    rate(tc, sources[0]['fps'])
    node(tc, 'frame', 0)
    node(tc, 'displayformat', 'NDF')
    media = node(sequence, 'media')
    video = node(media, 'video')
    sequence_source = sources[0] if sequence_size is None else {
        **sources[0], 'width': sequence_size[0], 'height': sequence_size[1], 'rotation_degrees': 0}
    video_format(node(video, 'format'), sequence_source)
    vt = node(video, 'track')
    audio = node(media, 'audio')
    channels = sources[0]['channels']
    node(audio, 'numOutputChannels', channels)
    sc = node(node(audio, 'format'), 'samplecharacteristics')
    node(sc, 'depth', 16)
    node(sc, 'samplerate', 48000)
    ats = [node(audio, 'track') for _ in range(channels * (2 if treated else 1))]
    cursor = clip_index = 0
    for source_index, source in enumerate(sources, 1):
        for segment_index, (start, end) in enumerate(source['kept_frames'], 1):
            clip_index += 1
            refs = [(f'v-{clip_index}', 'video', 1)] + [
                (f'a{ch}-{clip_index}', 'audio', ch) for ch in range(1, channels + 1)]
            if treated:
                refs += [(f't{ch}-{clip_index}', 'audio', channels + ch)
                         for ch in range(1, channels + 1)]
            for (ref, kind, channel), track in zip(refs, [vt] + ats):
                is_treated = kind == 'audio' and channel > channels
                clip_source = source['treated_audio'] if is_treated else source
                clip = node(track, 'clipitem', id=ref)
                node(clip, 'name', clip_source['name'])
                node(clip, 'enabled', 'FALSE' if treated and kind == 'audio' and not is_treated else 'TRUE')
                node(clip, 'duration', source['frames'])
                rate(clip, source['fps'])
                for key, value in [('start', cursor), ('end', cursor + end - start),
                                   ('in', start), ('out', end)]:
                    node(clip, key, value)
                source_file(clip, clip_source,
                            f'treated-{source_index}' if is_treated else f'file-{source_index}',
                            segment_index == 1 and (kind == 'video' or (is_treated and channel == channels + 1)))
                st = node(clip, 'sourcetrack')
                node(st, 'mediatype', kind)
                node(st, 'trackindex', channel - channels if is_treated else channel)
                for other_id, other_kind, other_track in refs:
                    link = node(clip, 'link')
                    node(link, 'linkclipref', other_id)
                    node(link, 'mediatype', other_kind)
                    node(link, 'trackindex', other_track)
                    node(link, 'clipindex', clip_index)
                    if other_kind == 'audio':
                        node(link, 'groupindex', 2 if other_track > channels else 1)
            cursor += end - start
    for channel, track in enumerate(ats, 1):
        node(track, 'enabled', 'TRUE')
        node(track, 'locked', 'FALSE')
        node(track, 'outputchannelindex', (channel - 1) % channels + 1)
    node(vt, 'enabled', 'TRUE')
    node(vt, 'locked', 'FALSE')
    ET.indent(root)
    return b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE xmeml>\n' + ET.tostring(root, encoding='utf-8')


def validate_xml(data, check_files=False):
    root = ET.fromstring(data)
    seq = root.find('sequence')
    ids = {c.attrib['id'] for c in seq.findall('.//clipitem')}
    files = {f.attrib['id'] for f in seq.findall('.//file') if f.find('pathurl') is not None}
    if check_files:
        for url in seq.findall('.//file/pathurl'):
            if not local_url_path(url.text).is_file():
                raise ValueError(f'Referência não encontrada: {url.text}')
    duration = int(seq.findtext('duration'))
    for track in seq.findall('./media/video/track') + seq.findall('./media/audio/track'):
        cursor = 0
        for clip in track.findall('clipitem'):
            start, end, sin, sout = [int(clip.findtext(k)) for k in ('start', 'end', 'in', 'out')]
            if start != cursor or end - start != sout - sin or not 0 <= sin < sout <= int(clip.findtext('duration')):
                raise ValueError('XML com duração ou continuidade inválida.')
            if clip.find('file').attrib['id'] not in files:
                raise ValueError('XML com referência de mídia inválida.')
            if any(link.findtext('linkclipref') not in ids for link in clip.findall('link')):
                raise ValueError('XML com vínculo inválido.')
            cursor = end
        if cursor != duration:
            raise ValueError('Faixas com durações diferentes.')


def append_report(folder, body):
    folder.mkdir(exist_ok=True)
    report(folder, body, title='Exportação XML', section=SECTION)


def process(edition, videos):
    began = time.monotonic()
    metadata, output_dir = edition / 'metadados', edition / 'premiere'
    with LiveProgress('1/4 Conferindo originais', inline=True):
        sources = [probe(p) for p in videos]
        profiles = {(s['fps'], s['width'], s['height'], s['channels']) for s in sources}
        if len(profiles) != 1:
            raise ValueError('O piloto exige mesmo FPS, resolução e número de canais na edição.')
    signature = {'config': CONFIG, 'sources': sources}
    state = metadata / 'estado-premiere.json'
    previous = None
    if state.exists():
        previous = json.loads(state.read_text(encoding='utf-8'))
        xml = edition / previous['output']
        plan = edition / previous['plan']
        if (previous['signature'] == signature and xml.is_file() and plan.is_file()
                and file_hash(xml) == previous['xml_sha256']
                and file_hash(plan) == previous['plan_sha256']
                and all((edition / item['path']).is_file() and
                        file_hash(edition / item['path']) == item['sha256']
                        for item in previous.get('derived_media', []))):
            say('Sem alterações; entrega anterior preservada.', 'yellow')
            return 'ignored'
    debug = io.StringIO()
    plans = reusable_analysis(previous, edition, sources)
    if plans is None:
        plans = []
        for i, source in enumerate(sources, 1):
            print(f"Arquivo {i}/{len(sources)}: {source['name']}", flush=True)
            plans.append(analyze(source, debug))
    else:
        say('Plano de cortes preservado.', 'green')
    metadata.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    version = 1
    while ((output_dir / f'timeline-v{version:03}.xml').exists() or
           (output_dir / f'timeline-v{version:03}.prproj').exists() or
           (metadata / f'plano-edicao-v{version:03}.json').exists() or
           (output_dir / 'audio' / f'v{version:03}').exists()):
        version += 1
    for index, source in enumerate(plans, 1):
        print(f"Arquivo {index}/{len(plans)}: {source['name']}", flush=True)
        destination = output_dir / 'audio' / f'v{version:03}' / f"{index:03} - {Path(source['name']).stem}.wav"
        source['treated_audio'] = write_treated_audio(source, destination, debug)
    with LiveProgress('4/4 Montando e conferindo XML', inline=True):
        data = build_xml(edition.name, plans)
        validate_xml(data, check_files=True)
        for source in sources:
            if file_hash(Path(source['path'])) != source['sha256']:
                raise ValueError('Original alterado durante o processamento; entrega cancelada.')
        xml = output_dir / f'timeline-v{version:03}.xml'
        plan = metadata / f'plano-edicao-v{version:03}.json'
        with xml.open('xb') as stream:
            stream.write(data)
        record = {'created_at': datetime.now().astimezone().isoformat(),
                  'config': CONFIG, 'sources': plans, 'xml': str(xml.relative_to(edition)),
                  'validation': 'estrutura verificada; importação no Premiere pendente'}
        with plan.open('x', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
        state_data = {'signature': signature, 'output': str(xml.relative_to(edition)),
                      'plan': str(plan.relative_to(edition)), 'xml_sha256': file_hash(xml),
                      'plan_sha256': file_hash(plan),
                      'derived_media': [{'path': str(Path(s['treated_audio']['path']).relative_to(edition)),
                                         'sha256': s['treated_audio']['sha256']} for s in plans]}
        temporary = state.with_suffix('.tmp')
        temporary.write_text(json.dumps(state_data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(state)
    cuts = sum(len(p['removed_frames']) for p in plans)
    original = sum(p['duration'] for p in plans)
    edited = sum((e - s) / float(Fraction(p['fps'])) for p in plans for s, e in p['kept_frames'])
    colors = '\n'.join(f"- `{s['name']}`: {s['color']}" for s in sources)
    append_report(metadata,
                  f'- Status: XML GERADO; importação no Premiere pendente.\n'
                  f'- Versão: v{version:03}; XML: `../premiere/{xml.name}`.\n'
                  f'- Arquivos (ordem): {", ".join(s["name"] for s in sources)}.\n'
                  f'- Pausas encurtadas: {cuts}; duração: {original:.3f}s → {edited:.3f}s.\n'
                  '- Regra: pausas acima de 200 ms, limiar −25 dB após normalização analítica de pico para −1 dB.\n'
                  '- Cortes limitados a frames inteiros, preservando pelo menos 200 ms nas pausas internas (pode sobrar até dois frames). Velocidade 1x.\n'
                  '- Vídeo original sem renderização. Áudio original com Enable desativado; WAV tratado ativo em faixas separadas, mesmos cortes e todos vinculados. Estéreo em dois canais por versão de áudio.\n'
                  '- Tratamento: redução espectral leve de ruído (3 dB), ganho fixo de pico para aproximadamente −1 dBFS, PCM 24-bit/48 kHz. Sem compressor, aceleração, correção de pitch ou remoção automática de eco.\n'
                  '- WAVs completos, incluindo pausas removidas: permitem estender os clipes sem perder áudio. Tratamento gravado no WAV; parâmetros de tratamento não são efeitos nativos editáveis. Original disponível para substituir.\n'
                  '- URLs locais no padrão file://localhost/C%3a/...; referências verificadas em disco. Relink automático no Premiere ainda precisa de teste; mover a entrega pode exigir relink.\n'
                  '- Sem transcrição, legendas, títulos ou eliminação de repetições nesta etapa.\n'
                  '- Cor: original preservada; nenhum tone mapping ou efeito aplicado. Conferir gerenciamento de cor no Premiere.\n'
                  '- Camada de ajuste/Lumetri: PENDENTE de XML e .prproj modelo exportados pelo Premiere; não substituída por vídeo renderizado ou efeito inventado. Sugestão provisória: vibrance +5, contraste +5, nitidez 5, vinheta −0,3.\n'
                  f'{colors}\n'
                  f'- Tempo total: {time.monotonic()-began:.1f}s.\n'
                  '- Teste: importar XML, abrir sequência, conferir imagem/áudio/cortes e estender um clipe para recuperar uma pausa. Salvar como .prproj.\n'
                  '- XML v001 de cortes importado com sucesso no Premiere 2026 pelo editor, com relink manual. Novo áudio/URLs ainda precisam de teste. Compatibilidade 2024/2025 pendente.')
    summary('XML pronto para testar', [f'Pausas encurtadas: {cuts}',
                                     f'Duração: {original:.1f}s → {edited:.1f}s',
                                     f'Resultado: {xml}', 'Relatório atualizado.'])
    return 'done'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clientes', type=Path, default=DEFAULT_CLIENTS)
    args = parser.parse_args()
    clients = args.clientes.resolve()
    if not clients.is_dir():
        parser.error(f'Pasta não encontrada: {clients}')
    say('pré-edição Premiere por Daniel Fileto')
    counts = {'done': 0, 'ignored': 0, 'error': 0}
    for edition, videos in discover(clients):
        say(f'\n{edition.parent.name} / {edition.name}')
        stage = 'xml'
        try:
            status = process(edition, videos)
            stage = 'legendas'
            from legendar_premiere import process as generate_subtitles
            generate_subtitles(edition)
            stage = 'editorial'
            from analise_editorial import prepare as prepare_editorial
            prepare_editorial(edition)
            counts[status] += 1
        except Exception as error:
            counts['error'] += 1
            say(f'ERRO: {error}', 'red')
            if stage == 'editorial':
                report(edition / 'metadados', f'- Status: ERRO NA ANÁLISE EDITORIAL\n- Detalhe: {error}\n- XML, áudio e legendas existentes preservados. Não aplicar cortes/títulos sem análise validada.',
                       title='Análise Gemini', section='## Direção editorial')
            elif stage == 'legendas':
                report(edition / 'metadados', f'- Status: ERRO\n- Detalhe: {error}\n- XML já gerado/preservado; falha apenas nas legendas.',
                       title='Legendas locais', section='## Legendas Premiere')
            else:
                append_report(edition / 'metadados', f'- Status: ERRO\n- Detalhe: {error}')
    summary('Processamento encerrado', [f"Concluídos: {counts['done']} | Sem alterações: {counts['ignored']} | Erros: {counts['error']}"])
    say('Análise editorial prepara sugestões; montagem gráfica nativa ainda depende do modelo de título.', 'yellow')
    return int(counts['error'] > 0)


if __name__ == '__main__':
    sys.exit(main())
