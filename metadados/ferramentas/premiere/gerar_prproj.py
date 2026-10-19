"""Piloto nativo baseado no modelo do editor; não é exportador genérico de .prproj.

Preserva o grafo do projeto e só aceita fontes/cortes/legendas compatíveis com o
modelo. Grava nova versão, nunca o modelo ou entregas anteriores.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import copy
from datetime import datetime
from fractions import Fraction
import gzip
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import uuid
import xml.etree.ElementTree as ET

from exportar_premiere import (ROOT, DEFAULT_CLIENTS, CONFIG, file_hash, report,
                               write_treated_audio)
from console_pipeline import LiveProgress, say, summary

TICKS = 254016000000
SECTION = '## Projeto nativo Premiere'


def read_project(path):
    return ET.fromstring(gzip.decompress(path.read_bytes()))


def object_index(root):
    return {element.attrib['ObjectID']: element for element in root if 'ObjectID' in element.attrib}


def uid_index(root):
    return {element.attrib['ObjectUID']: element for element in root if 'ObjectUID' in element.attrib}


def set_text(parent, name, value):
    element = parent.find(name)
    if element is None:
        element = ET.SubElement(parent, name)
    element.text = str(value)
    return element


def track_items(track, objects):
    return [objects[ref.attrib['ObjectRef']] for ref in track.findall('.//ClipItems/TrackItems/TrackItem')]


def item_clip(item, objects):
    sub = objects[item.find('.//SubClip').attrib['ObjectRef']]
    return objects[sub.find('Clip').attrib['ObjectRef']]


def clip_media(clip, objects, uids):
    source = objects[clip.find('.//Clip/Source').attrib['ObjectRef']]
    return uids[source.find('.//Media').attrib['ObjectURef']]


def timing(item):
    element = item.find('.//TrackItem')
    return int(element.findtext('Start', '0')), int(element.findtext('End'))


def reference_graph(root):
    # Inclui referências internas com escopos próprios sem renumerá-las.
    return Counter((element.tag, key, value) for element in root.iter()
                   for key, value in element.attrib.items()
                   if key in ('ObjectID', 'ObjectUID', 'ObjectRef', 'ObjectURef'))


def native_blobs(root):
    return {element.attrib['BinaryHash']: base64.b64decode(element.text)
            for element in root.iter() if element.attrib.get('Encoding') == 'base64'
            and element.attrib.get('BinaryHash') and element.text and element.text.strip()}


def lumetri_component(root):
    for effect in root.findall('VideoFilterComponent'):
        if effect.findtext('.//MatchName') == 'AE.ADBE Lumetri':
            return effect.find('Component')
    return None


def parse_srt(text):
    cues = []
    text = text.lstrip('\ufeff').replace('\r\n', '\n')
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = block.splitlines()
        if len(lines) < 3:
            raise ValueError('Bloco SRT incompleto.')
        match = re.fullmatch(r'(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)', lines[1])
        if not match:
            raise ValueError('Timestamp SRT não reconhecido.')
        values = list(map(int, match.groups()))
        def millis(parts):
            h, m, s, ms = parts
            return ((h * 60 + m) * 60 + s) * 1000 + ms
        start, end = millis(values[:4]), millis(values[4:])
        caption = '\n'.join(lines[2:])
        if not caption.strip() or len(caption.splitlines()) > 2 or start >= end:
            raise ValueError('As legendas devem ter texto e no máximo duas linhas.')
        if cues and start < cues[-1]['end_ms']:
            raise ValueError('Legendas sobrepostas no SRT.')
        cues.append({'text': caption, 'start_ms': start, 'end_ms': end})
    if not cues:
        raise ValueError('SRT vazio.')
    return cues


def check_model(root, source, cues):
    objects, uids = object_index(root), uid_index(root)
    if len(root.findall('Sequence')) != 1:
        raise ValueError('O piloto exige uma única sequência no modelo.')
    group = root.find('VideoTrackGroup')
    fps = Fraction(source['fps'])
    ticks_per_frame = Fraction(TICKS, 1) / fps
    if ticks_per_frame.denominator != 1 or int(group.findtext('./TrackGroup/FrameRate')) != ticks_per_frame:
        raise ValueError('FPS do modelo não corresponde à fonte.')
    if group.findtext('FrameRect') != f"0,0,{source['width']},{source['height']}":
        raise ValueError('Resolução do modelo não corresponde à fonte.')
    tracks = root.findall('VideoClipTrack')
    if len(tracks) != 2 or len(root.findall('AudioClipTrack')) != 4 or source['channels'] != 2:
        raise ValueError('Este piloto usa o modelo com V1, ajuste V2 e quatro canais de áudio.')
    video_items = track_items(tracks[0], objects)
    all_tracks = [video_items] + [track_items(t, objects) for t in root.findall('AudioClipTrack')]
    cursor = 0
    for items in all_tracks:
        if len(items) != len(source['kept_frames']):
            raise ValueError('Modelo com quantidade de cortes diferente; não adaptar às cegas.')
        cursor = 0
        for item, (start, end) in zip(items, source['kept_frames']):
            clip = item_clip(item, objects)
            expected_in, expected_out = int(start * ticks_per_frame), int(end * ticks_per_frame)
            if (int(clip.findtext('.//Clip/InPoint', '0')) != expected_in or
                    int(clip.findtext('.//Clip/OutPoint')) != expected_out or
                    timing(item) != (cursor, cursor + expected_out - expected_in)):
                raise ValueError('Os cortes do modelo não correspondem ao plano aprovado.')
            cursor += expected_out - expected_in
    media = clip_media(item_clip(video_items[0], objects), objects, uids)
    original = Path(media.findtext('FilePath'))
    if not original.is_file() or file_hash(original) != source['sha256']:
        raise ValueError('O modelo não aponta para o mesmo vídeo original.')
    adjustments = track_items(tracks[1], objects)
    if len(adjustments) != 1:
        raise ValueError('Esperada uma camada de ajuste no modelo.')
    adjustment = item_clip(adjustments[0], objects)
    if adjustment.findtext('AdjustmentLayer') != 'true':
        raise ValueError('A camada do modelo não é um ajuste nativo.')
    captions = root.findall('CaptionDataClipTrack')
    if len(captions) != 1:
        raise ValueError('Esperada uma faixa nativa de legendas.')
    caption_items = track_items(captions[0], objects)
    if len(caption_items) != len(cues):
        raise ValueError('Legendas do modelo diferentes do SRT; piloto não suporta transplantar texto arbitrário.')
    blobs = native_blobs(root)
    for item, cue in zip(caption_items, cues):
        data = objects[item.find('./BlockVector/BlockVectorItem').attrib['ObjectRef']].find('FormattedTextData')
        blob = blobs[data.attrib['BinaryHash']]
        wanted = cue['text'].replace('\n', '\r').encode('utf-8')
        position = blob.find(wanted)
        if position < 4 or int.from_bytes(blob[position-4:position], 'little') != len(wanted):
            raise ValueError('Texto nativo não corresponde ao SRT; não modificar blobs de texto sem validação.')
    lumetri = lumetri_component(root)
    if lumetri is None:
        raise ValueError('Lumetri nativo não encontrado no modelo.')
    return cursor, int(ticks_per_frame)


def lumetri_values(root):
    objects = object_index(root)
    component = lumetri_component(root)
    values = {}
    for ref in component.findall('./Params/Param'):
        param = objects[ref.attrib['ObjectRef']]
        ident = param.findtext('ParameterID')
        if ident in ('12', '13', '29', '30', '51', '52', '53', '54'):
            current = param.findtext('CurrentValue')
            if current is None:
                current = param.findtext('StartKeyframe').split(',')[1]
            values[ident] = {'name': param.findtext('Name'), 'value': float(current)}
    return values


def unity_audio(root):
    """0 dB conforme preset nativo salvo pelo editor, não valor normalizado 1.

    Em Level, CurrentValue=1 foi exibido como +15 dB. O projeto corrigido pelo
    Premiere omite CurrentValue/StartKeyframe no estado padrão de 0 dB.
    """
    changed = 0
    previous_values = []
    for param in root.findall('AudioComponentParam'):
        if param.findtext('Name') not in ('Level', 'Volume'):
            continue
        if param.find('Keyframes') is not None:
            raise ValueError('Automação de volume no modelo requer revisão antes de zerar o ganho.')
        value = param.findtext('CurrentValue')
        if value:
            previous_values.append(float(value))
        for tag in ('CurrentValue', 'StartKeyframe'):
            element = param.find(tag)
            if element is not None:
                param.remove(element)
        changed += 1
    return changed, previous_values


def relink_media(root, output_dir, source, treated_path, srt_path):
    replacements = []
    for media in root.findall('Media'):
        old_text = media.findtext('FilePath')
        if not old_text or not Path(old_text).suffix:
            continue  # Identificadores sintéticos do próprio Premiere, não arquivos.
        old = Path(old_text)
        if old.suffix.lower() in ('.mov', '.mp4'):
            new = Path(source['path'])
        elif old.suffix.lower() == '.wav':
            new = treated_path
        elif old.suffix.lower() == '.srt':
            new = srt_path
        else:
            raise ValueError(f'Mídia externa inesperada no modelo: {old}')
        replacements.append((old, new))
        for tag in ('FilePath', 'ActualMediaFilePath'):
            set_text(media, tag, str(new.resolve()))
        set_text(media, 'RelativePath', os.path.relpath(new, output_dir))
        set_text(media, 'Title', new.name)
        if old != new and old.suffix.lower() == '.wav':
            set_text(media, 'FileKey', str(uuid.uuid4()))
            set_text(media, 'ContentAndMetadataState', str(uuid.uuid4()))
            modification = media.find('ModificationState')
            if modification is not None:
                media.remove(modification)
            for stream_ref in media.findall('AudioStream'):
                stream = object_index(root)[stream_ref.attrib['ObjectRef']]
                for tag in ('PeakFilePath', 'ConformedAudioPath'):
                    cache = stream.find(tag)
                    if cache is not None:
                        stream.remove(cache)
    if len(replacements) != 3:
        raise ValueError('Esperadas exatamente três mídias reais no modelo (vídeo, WAV e SRT).')
    # Nomes de itens/mídias, não blobs nem substituição global de números de versão.
    names = {old.name: new.name for old, new in replacements}
    for element in root.iter():
        if element.tag in ('Name', 'ClipName') and element.text in names:
            element.text = names[element.text]


def adapt_project(model, source, cues, output_dir, treated_path, srt_path, version):
    root = copy.deepcopy(model)
    duration, ticks_per_frame = check_model(root, source, cues)
    before_graph, before_lumetri = reference_graph(root), lumetri_values(root)
    objects = object_index(root)
    adjustment_item = track_items(root.findall('VideoClipTrack')[1], objects)[0]
    adjustment_clip = item_clip(adjustment_item, objects)
    set_text(adjustment_item.find('.//TrackItem'), 'Start', 0)
    set_text(adjustment_item.find('.//TrackItem'), 'End', duration)
    source_start = int(adjustment_clip.findtext('./Clip/InPoint', '0'))
    set_text(adjustment_clip.find('Clip'), 'OutPoint', source_start + duration)
    # A duração do projeto deixa de ser estendida pela camada de ajuste do modelo.
    for sequence_source in root.findall('VideoSequenceSource') + root.findall('AudioSequenceSource'):
        set_text(sequence_source, 'OriginalDuration', duration)
    sequence = root.find('Sequence')
    old_name = sequence.findtext('Name')
    new_name = f'{old_name} - v{version:03}'
    for element in root.iter():
        if element.tag == 'Name' and element.text == old_name:
            element.text = new_name
    properties = sequence.find('./Node/Properties')
    set_text(properties, 'Monitor.ProgramZoomOut', duration)
    set_text(properties, 'Monitor.ProgramZoomIn', 0)
    set_text(properties, 'MZ.EditLine', 0)
    audio_tracks = root.findall('AudioClipTrack')
    for index, track in enumerate(audio_tracks):
        for item in track_items(track, objects):
            set_text(item.find('ClipTrackItem'), 'IsMuted', 'true' if index < 2 else 'false')
    caption_items = track_items(root.find('CaptionDataClipTrack'), objects)
    for item, cue in zip(caption_items, cues):
        start_frame = cue['start_ms'] * TICKS // (1000 * ticks_per_frame)
        end_frame = cue['end_ms'] * TICKS // (1000 * ticks_per_frame)
        if end_frame <= start_frame:
            raise ValueError('Legenda curta demais para o FPS do projeto.')
        set_text(item.find('.//TrackItem'), 'Start', start_frame * ticks_per_frame)
        set_text(item.find('.//TrackItem'), 'End', end_frame * ticks_per_frame)
        # Mantém o offset sintético nativo do transcript, ajustando só os limites.
        clip = item_clip(item, objects).find('.//Clip')
        old_in = int(clip.findtext('InPoint'))
        set_text(clip, 'OutPoint', old_in + (end_frame - start_frame) * ticks_per_frame)
    changed, previous = unity_audio(root)
    relink_media(root, output_dir, source, treated_path, srt_path)
    if reference_graph(root) != before_graph or lumetri_values(root) != before_lumetri:
        raise ValueError('Referências internas ou preset Lumetri alterados indevidamente.')
    return root, {'duration_ticks': duration, 'duration_seconds': duration / TICKS,
                  'captions': len(cues), 'volume_params_unity': changed,
                  'previous_volume_values': sorted(set(previous)),
                  'lumetri': before_lumetri}


def next_version(edition):
    versions = [2]
    for path in (edition / 'premiere').glob('*'):
        match = re.search(r'(?:timeline|legendas-timeline)-v(\d+)', path.stem)
        if match:
            versions.append(int(match.group(1)))
    for path in (edition / 'premiere' / 'audio').glob('v*'):
        if re.fullmatch(r'v\d+', path.name):
            versions.append(int(path.name[1:]))
    for path in (edition / 'metadados').glob('*v*.json'):
        match = re.search(r'-v(\d+)', path.stem)
        if match:
            versions.append(int(match.group(1)))
    return max(versions) + 1


def create(edition, model_path):
    metadata, output_dir = edition / 'metadados', edition / 'premiere'
    state = json.loads((metadata / 'estado-premiere.json').read_text(encoding='utf-8'))
    base_path = edition / state['plan']
    if file_hash(base_path) != state['plan_sha256']:
        raise ValueError('Plano-base alterado externamente.')
    plan = json.loads(base_path.read_text(encoding='utf-8'))
    if len(plan['sources']) != 1:
        raise ValueError('Este primeiro piloto nativo aceita um único original.')
    source = plan['sources'][0]
    if file_hash(Path(source['path'])) != source['sha256']:
        raise ValueError('Original alterado desde a pré-edição.')
    subtitle_state = json.loads((metadata / 'estado-legendas.json').read_text(encoding='utf-8'))
    if subtitle_state['signature']['plan_sha256'] != state['plan_sha256']:
        raise ValueError('SRT não corresponde ao plano-base.')
    base_srt = edition / subtitle_state['output']
    if file_hash(base_srt) != subtitle_state['sha256']:
        raise ValueError('SRT alterado externamente; revise antes de gerar o projeto.')
    text = base_srt.read_text(encoding='utf-8-sig')
    cues = parse_srt(text)
    model_hash = file_hash(model_path)
    model = read_project(model_path)
    version = next_version(edition)
    project_path = output_dir / f'timeline-v{version:03}.prproj'
    srt_path = output_dir / f'legendas-timeline-v{version:03}.srt'
    wav_path = output_dir / 'audio' / f'v{version:03}' / f"001 - {Path(source['name']).stem}-v{version:03}.wav"
    with LiveProgress('1/4 Adaptando projeto nativo', inline=True):
        native, details = adapt_project(model, source, cues, output_dir, wav_path, srt_path, version)
    audio_options = {**CONFIG['audio'], 'peak_target_dbfs': 0.0}
    treated = write_treated_audio(source, wav_path, io.StringIO(), audio_options=audio_options)
    with LiveProgress('4/4 Conferindo e salvando projeto', inline=True):
        if file_hash(model_path) != model_hash or file_hash(Path(source['path'])) != source['sha256']:
            raise ValueError('Modelo ou original mudou durante o processo.')
        with srt_path.open('x', encoding='utf-8-sig', newline='\n') as stream:
            stream.write(text)
        # Gzip + XML nativo; não é um XML de intercâmbio renomeado.
        data = ET.tostring(native, encoding='utf-8', xml_declaration=True)
        compressed = gzip.compress(data, mtime=0)
        parsed = ET.fromstring(gzip.decompress(compressed))
        if reference_graph(parsed) != reference_graph(model):
            raise ValueError('Projeto não passou pela conferência de referências.')
        for media in parsed.findall('Media'):
            path = media.findtext('FilePath')
            if path and Path(path).suffix and not Path(path).is_file():
                raise ValueError(f'Mídia ausente no projeto: {path}')
        with project_path.open('xb') as stream:
            stream.write(compressed)
        record = {'version': version, 'created_at': datetime.now().astimezone().isoformat(),
                  'model': str(model_path), 'model_sha256': model_hash,
                  'base_plan': str(base_path), 'source_sha256': source['sha256'],
                  'project': str(project_path), 'project_sha256': file_hash(project_path),
                  'srt': str(srt_path), 'audio': treated, 'audio_options': audio_options,
                  'checks': details, 'status': 'teste estrutural aprovado; abertura no Premiere pendente',
                  'scope': 'piloto restrito ao modelo desta edição; não é gerador genérico'}
        record_path = metadata / f'plano-nativo-v{version:03}.json'
        with record_path.open('x', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
    report(metadata,
           f'- Status: PRPROJ v{version:03} GERADO; abertura no Premiere 2026 pendente.\n'
           f'- Projeto: `../premiere/{project_path.name}`; duração {details["duration_seconds"]:.3f}s, 1×.\n'
           '- Cortes aprovados preservados e conferidos em todas as faixas, com acesso às mídias completas.\n'
           '- V1: original. V2: camada de ajuste NATIVA, duração ajustada ao vídeo, Lumetri preservado do modelo.\n'
           '- Preset: contraste 40, altas luzes −10, nitidez 40, vibrance 30, vinheta −1 (ponto médio 50, arredondamento 0, suavização 50).\n'
           '- Áudio original desativado em A1/A2; WAV tratado em A3/A4, redução de ruído leve, pico-alvo 0 dBFS, sem mudança de velocidade/pitch.\n'
           '- Level de clipe/faixa no padrão nativo de 0 dB, sem escrever CurrentValue=1 (esse valor havia sido exibido como +15 dB no teste anterior). Não é normalização de LUFS; não houve compressão ou remoção de eco.\n'
           f'- {len(cues)} legendas nativas incluídas, com no máximo duas linhas explícitas, texto e estilo do modelo preservados. Conferir eventuais quebras automáticas no monitor do Premiere. SRT v{version:03} também entregue.\n'
           '- Referências internas conferidas e preset idêntico ao modelo; isso não substitui o teste de abertura/reprodução no aplicativo.\n'
           '- Originais, modelo e versões anteriores intactos. Nenhum XML novo gerado.\n'
           '- Piloto específico para esta edição/modelo. Automação genérica e integração desse novo exportador no BAT ainda dependem da aprovação do teste.',
           title=f'Projeto v{version:03}', section=SECTION)
    summary('Projeto nativo pronto para testar', [str(project_path),
                                              f'Legendas nativas: {len(cues)} | Pico-alvo: 0 dBFS',
                                              'Modelo e versões anteriores preservados.'])
    return project_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edition', type=Path, required=True)
    parser.add_argument('--model', type=Path, default=DEFAULT_CLIENTS / 'metadados' / 'prproj' / 'estudo-original' / 'modelo.prproj')
    args = parser.parse_args()
    try:
        create(args.edition.resolve(), args.model.resolve())
        return 0
    except Exception as error:
        say(f'ERRO: {error}', 'red')
        metadata = args.edition.resolve() / 'metadados'
        if metadata.is_dir():
            report(metadata, f'- Status: ERRO\n- Detalhe: {error}', title='Teste nativo', section=SECTION)
        return 1


if __name__ == '__main__':
    sys.exit(main())
