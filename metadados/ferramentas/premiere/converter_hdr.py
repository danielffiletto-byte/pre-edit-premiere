"""Conversão Premiere HLG→SDR aprovada; original intacto, cache verificado."""
import hashlib
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import subprocess

from controle_execucao import pending_files, control


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def hdr_matches(source):
    color = source.get('color', {})
    return (color.get('color_transfer') == 'arib-std-b67'
            and color.get('color_primaries') == 'bt2020'
            and color.get('color_space') in {'bt2020nc', 'bt2020c'}
            and '10' in source.get('pixel_format', ''))


def generated_sdr(path):
    # Prefixo reservado exclusivamente às cópias produzidas pelo pré-editor.
    return Path(path).name.casefold().startswith('sdr ')


def select_encoder(preset):
    result = subprocess.run([shutil.which('ffmpeg'), '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', 'color=size=256x256:rate=1,format=p010le', '-frames:v', '1',
        *preset['gpu_encoder'], '-f', 'null', '-'], capture_output=True, timeout=30)
    return preset['gpu_encoder'] if result.returncode == 0 else preset['cpu_encoder']


def validate_conversion(original, converted):
    if (converted.get('pixel_format') != 'yuv420p10le'
            or any(converted['color'].get(k) != 'bt709' for k in
                   ('color_space', 'color_transfer', 'color_primaries'))):
        raise ValueError('Conversão não confirmou Rec.709/10 bits.')
    if (converted['channels'] != original['channels']
            or converted['sample_rate'] != original['sample_rate']
            or converted['decoded_frames'] != original['decoded_frames']
            or abs(converted['duration']-original['duration']) > 1/float(Fraction(original['fps']))):
        raise ValueError('Conversão alterou contagem/duração ou formato do áudio; revisar.')
    rotated = abs(original.get('rotation_degrees', 0)) % 180 == 90
    size = (original['height'], original['width']) if rotated else (original['width'], original['height'])
    if (converted['width'], converted['height']) != size or converted.get('rotation_degrees', 0) != 0:
        raise ValueError('Conversão alterou resolução/orientação inesperadamente.')


def save_record(cache, record):
    temporary = cache.with_suffix('.json.tmp')
    with pending_files([temporary]):
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(cache)


def reuse_existing(source, destination, metadata, cache, key, run, probe, file_hash):
    """Reutiliza SDR válido mesmo quando a pasta de metadados foi removida."""
    output_hash = file_hash(destination)
    saved = None
    if cache.exists():
        saved = json.loads(cache.read_text(encoding='utf-8'))
        if saved.get('key') != key or output_hash != saved.get('output_sha256'):
            raise ValueError(f'{destination.name}: cópia SDR alterada; nada sobrescrito.')
    else:
        # O caminho/preset pode ter mudado: não perder uma conversão conhecida.
        for previous in sorted(metadata.glob('hdr-sdr-*.json')):
            candidate = json.loads(previous.read_text(encoding='utf-8'))
            if Path(candidate.get('output', '')).name.casefold() != destination.name.casefold():
                continue
            if candidate.get('original_source', {}).get('sha256') != source['sha256']:
                raise ValueError(f'{destination.name}: o original mudou; cópia SDR preservada para revisão.')
            if candidate.get('output_sha256') != output_hash:
                raise ValueError(f'{destination.name}: cópia SDR alterada; nada sobrescrito.')
            saved = candidate
    converted = probe(destination)
    validate_conversion(source, converted)
    if saved is None or saved.get('full_decode') != 'OK':
        with (metadata/'hdr-sdr-debug.log').open('a', encoding='utf-8') as debug:
            # Somente decodifica para conferir integridade; não converte nem grava vídeo.
            run(['-v', 'error', '-xerror', '-i', str(destination), '-map', '0:v:0',
                 '-map', '0:a:0', '-f', 'null', '-'],
                'Conferindo SDR existente — sem reconverter', source['duration'], debug)
    if file_hash(Path(source['path'])) != source['sha256'] or file_hash(destination) != output_hash:
        raise ValueError('Arquivo mudou durante a conferência SDR; nada sobrescrito.')
    record = {**(saved or {}), 'key': key, 'original_source': source,
              'output': str(destination), 'output_sha256': output_hash, 'full_decode': 'OK'}
    if saved is None:
        # Rec.709/integridade podem ser conferidos; o look não é deduzido do arquivo.
        record.update(origin='existing_sdr', preset=None, preset_verified=False, encoder=None)
    save_record(cache, record)
    converted['hdr_conversion'] = record
    converted['sdr_reused'] = True
    return converted


def prepare(source, metadata, preset, run, probe, file_hash):
    if not preset.get('enabled', True) or not hdr_matches(source):
        return source
    original = Path(source['path'])
    destination = original.with_name(f'sdr {original.stem}.mp4')
    if os.environ.get('PREEDITOR_METADATA_ROOT'):
        media = metadata/'Midia'
        media.mkdir(exist_ok=True)
        destination = media/f'sdr {original.stem}-{source["sha256"][:8]}.mp4'
    key = fingerprint({'source': source['sha256'], 'path': str(original), 'preset': preset})
    cache = metadata/f'hdr-sdr-{key[:20]}.json'
    legacy_sdr = original.with_name(f'sdr {original.stem}.mp4')
    if os.environ.get('PREEDITOR_METADATA_ROOT') and not destination.exists() and legacy_sdr.is_file():
        # Migração para Metadados: aproveitar a cópia anterior, nunca recodificá-la.
        with pending_files([destination, cache, cache.with_suffix('.json.tmp')]):
            shutil.copy2(legacy_sdr, destination)
            return reuse_existing(source, destination, metadata, cache, key, run, probe, file_hash)
    if destination.exists():
        return reuse_existing(source, destination, metadata, cache, key, run, probe, file_hash)
    else:
        encoder = select_encoder(preset)
        temporary = metadata/f'hdr-sdr-{key[:20]}.partial.mp4'
        if temporary.exists():
            raise ValueError(f'Intermediário anterior encontrado: {temporary.name}; revise antes de repetir.')
        with pending_files([temporary, cache, cache.with_suffix('.json.tmp')]):
            with (metadata/'hdr-sdr-debug.log').open('a', encoding='utf-8') as debug:
                run(['-n', '-init_hw_device', 'vulkan=vk:0', '-filter_hw_device', 'vk',
                     '-i', str(original), '-map', '0:v:0', '-map', '0:a:0',
                     '-vf', preset['filter']+',format=p010le', '-fps_mode', 'passthrough',
                     *encoder, '-tag:v', 'hvc1', '-c:a', 'copy', '-map_metadata', '-1',
                     '-map_chapters', '-1', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                     '-colorspace', 'bt709', '-color_range', 'tv', '-movflags', '+faststart',
                     str(temporary)], 'Convertendo HDR para Rec.709 — cor aprovada', source['duration'], debug)
                converted = probe(temporary)
                validate_conversion(source, converted)
                run(['-v', 'error', '-xerror', '-i', str(temporary), '-map', '0:v:0',
                     '-map', '0:a:0', '-f', 'null', '-'], 'Conferindo conversão SDR', source['duration'], debug)
            if file_hash(original) != source['sha256']:
                raise ValueError('O original mudou durante a conversão.')
            record = {'key': key, 'preset': preset['preset'], 'preset_verified': True, 'original_source': source,
                      'output': str(destination), 'output_sha256': file_hash(temporary),
                      'encoder': encoder, 'full_decode': 'OK'}
            # ESC aguarda a publicação curta. O MP4 parcial fica apenas nos metadados.
            control(phase='publishing')
            temporary.rename(destination)  # No Windows, nunca substitui destino existente.
            cache.with_suffix('.json.tmp').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            cache.with_suffix('.json.tmp').replace(cache)
        control(phase='processing')
        converted = probe(destination)
    converted['hdr_conversion'] = record
    converted['sdr_reused'] = False
    return converted
