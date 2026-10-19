"""Piloto de títulos editáveis: duplica gráficos nativos do projeto aprovado.

Não é exportador genérico nem leitor universal de MOGRT. Aceita somente texto
com um run de estilo, preservando o restante do FlatBuffer e seus offsets.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime
from fractions import Fraction
import gzip
import json
import math
from pathlib import Path
import re
import struct
import textwrap
import uuid
import xml.etree.ElementTree as ET

from gerar_prproj import (TICKS, read_project, object_index, native_blobs,
                         track_items, item_clip, timing, set_text, lumetri_values,
                         next_version, file_hash, report)


def uint(blob, position):
    if position < 0 or position + 4 > len(blob):
        raise ValueError('Offset de texto fora do buffer.')
    return struct.unpack_from('<I', blob, position)[0]


def field(blob, table, index):
    if table < 0 or table + 4 > len(blob):
        raise ValueError('Tabela de texto inválida.')
    vtable = table - struct.unpack_from('<i', blob, table)[0]
    if vtable < 0 or vtable + 4 > len(blob):
        raise ValueError('Vtable inválida.')
    size = struct.unpack_from('<H', blob, vtable)[0]
    entry = vtable + 4 + index * 2
    if entry + 2 > vtable + size or entry + 2 > len(blob):
        raise ValueError('Campo de texto ausente.')
    offset = struct.unpack_from('<H', blob, entry)[0]
    if not offset:
        raise ValueError('Campo de texto vazio.')
    return table + offset


def pointed(blob, position):
    target = position + uint(blob, position)
    if target + 4 > len(blob):
        raise ValueError('Referência de texto inválida.')
    return target


def text_run(blob):
    if (len(blob) < 32 or uint(blob, 0) != len(blob) - 12 or
            uint(blob, 8) != 0x11223344):
        raise ValueError('Formato de texto nativo não reconhecido.')
    root = pointed(blob, 12)
    style = pointed(blob, field(blob, root, 0))
    runs = pointed(blob, field(blob, style, 0))
    if uint(blob, runs) != 1:
        raise ValueError('Texto com múltiplos estilos exige outro adaptador.')
    run = pointed(blob, runs + 4)
    pointer = field(blob, run, 0)
    string = pointed(blob, pointer)
    size = uint(blob, string)
    end = string + 4 + size
    if end >= len(blob) or blob[end] != 0:
        raise ValueError('String de texto inválida.')
    return pointer, blob[string + 4:end].decode('utf-8')


def replace_text(blob, text):
    if not text or '\0' in text:
        raise ValueError('Título vazio ou com caractere inválido.')
    pointer, _ = text_run(blob)
    # Append-only: nenhum objeto/vtable/estilo/keyframe existente é deslocado.
    output = bytearray(blob)
    output.extend(b'\0' * (-len(output) % 4))
    destination = len(output)
    encoded = text.replace('\n', '\r').encode('utf-8')
    output.extend(struct.pack('<I', len(encoded)) + encoded + b'\0')
    output.extend(b'\0' * (-len(output) % 4))
    struct.pack_into('<I', output, pointer, destination - pointer)
    struct.pack_into('<I', output, 0, len(output) - 12)
    result = bytes(output)
    if text_run(result)[1] != text.replace('\n', '\r'):
        raise ValueError('Texto não passou pela releitura.')
    return result


def text_effect(item, objects):
    chain = objects[item.find('.//Components').get('ObjectRef')]
    effects = [objects[e.get('ObjectRef')] for e in chain.findall('.//Components/Component')]
    texts = [e for e in effects if e.findtext('MatchName') == 'AE.ADBE Text']
    if len(texts) != 1:
        return None
    for ref in texts[0].findall('./Component/Params/Param'):
        param = objects[ref.get('ObjectRef')]
        if param.findtext('Name') == 'Source Text':
            return texts[0], param.find('StartKeyframeValue')
    raise ValueError('Source Text não encontrado.')


def inspect_templates(root):
    objects, blobs = object_index(root), native_blobs(root)
    found = {}
    for track in root.findall('VideoClipTrack'):
        for item in track_items(track, objects):
            effect = text_effect(item, objects)
            if effect is None:
                continue
            kind = 'mograph' if item.find('.//BE.Mogrt.LicenseID') is not None else 'simples'
            if kind in found:
                raise ValueError(f'Mais de um modelo {kind}; escolha explícita necessária.')
            _, value = effect
            found[kind] = {'item': item.get('ObjectID'),
                           'text': text_run(blobs[value.get('BinaryHash')])[1]}
    if set(found) != {'simples', 'mograph'}:
        raise ValueError('Esperados exatamente os dois gráficos fornecidos pelo editor.')
    return found


def update_text(root, item, text):
    effect, value = text_effect(item, object_index(root))
    blob = native_blobs(root)[value.get('BinaryHash')]
    value.set('BinaryHash', str(uuid.uuid4()))
    value.text = base64.b64encode(replace_text(blob, text)).decode('ascii')
    set_text(effect.find('Component'), 'InstanceName', text)
    objects = object_index(root)
    sub = objects[item.find('.//SubClip').get('ObjectRef')]
    set_text(sub, 'Name', text.replace('\n', ' '))


def clone_graphic(root, item_id):
    objects = object_index(root)
    owned = set()

    def visit(ident):
        if ident in owned:
            return
        obj = objects[ident]
        # Fontes e MasterClips são compartilhados, efeitos e parâmetros não.
        if obj.tag in ('VideoMediaSource', 'AudioMediaSource', 'Media'):
            return
        owned.add(ident)
        for element in obj.iter():
            ref = element.get('ObjectRef')
            if ref:
                if ref not in objects:
                    raise ValueError('Referência local não suportada neste modelo gráfico.')
                visit(ref)

    visit(item_id)
    first = max(map(int, objects)) + 1
    mapping = {old: str(first + i) for i, old in enumerate(sorted(owned, key=int))}
    clones = {}
    for ident in sorted(owned, key=int):
        clone = copy.deepcopy(objects[ident])
        clone.set('ObjectID', mapping[ident])
        for element in clone.iter():
            ref = element.get('ObjectRef')
            if ref in mapping:
                element.set('ObjectRef', mapping[ref])
            if element.tag == 'ClipID':
                element.text = str(uuid.uuid4())
        clones[ident] = clone
        root.append(clone)
    return clones[item_id]


def new_track(root, items):
    tracks = root.findall('VideoClipTrack')
    track = copy.deepcopy(tracks[0])
    track.set('ObjectUID', str(uuid.uuid4()))
    index = len(tracks)
    group = root.find('VideoTrackGroup/TrackGroup')
    track_id = int(group.findtext('NextTrackID'))
    set_text(group, 'NextTrackID', track_id + 1)
    set_text(track.find('./ClipTrack/Track'), 'Index', index)
    set_text(track.find('./ClipTrack/Track'), 'ID', track_id)
    for name in ('ClipItems', 'TransitionItems'):
        container = track.find(f'./ClipTrack/{name}')
        set_text(container, 'Index', index)
        for child in list(container):
            if child.tag in ('TrackItems', 'TransitionItems'):
                container.remove(child)
    refs = ET.SubElement(track.find('./ClipTrack/ClipItems'), 'TrackItems', Version='1')
    for i, item in enumerate(items):
        ET.SubElement(refs, 'TrackItem', Index=str(i), ObjectRef=item.get('ObjectID'))
    ET.SubElement(group.find('Tracks'), 'Track', Index=str(index), ObjectURef=track.get('ObjectUID'))
    root.append(track)
    return track


def unchanged_payload(root):
    tags = ('AudioClipTrack', 'AudioClipTrackItem', 'AudioClip', 'AudioComponentParam',
            'CaptionDataClipTrack', 'CaptionDataClipTrackItem', 'Block')
    return [ET.tostring(obj) for obj in root if obj.tag in tags], lumetri_values(root)


def build(model, editorial, mode, version):
    root = copy.deepcopy(model)
    objects = object_index(root)
    templates = inspect_templates(root)
    baseline = unchanged_payload(root)
    main_track = root.findall('VideoClipTrack')[0]
    sample_ids = {value['item'] for value in templates.values()}
    refs = main_track.find('.//ClipItems/TrackItems')
    for ref in list(refs):
        if ref.get('ObjectRef') in sample_ids:
            refs.remove(ref)
    for i, ref in enumerate(refs):
        ref.set('Index', str(i))
    video_items = track_items(main_track, objects)
    duration = max(timing(item)[1] for item in video_items)
    # Não use títulos com um plano de cortes diferente do material desta cópia.
    sources = editorial['sources']
    if len(sources) != 1 or len(video_items) != len(sources[0]['kept_frames']):
        raise ValueError('Plano editorial e projeto têm cortes diferentes.')
    frame = int(root.findtext('VideoTrackGroup/TrackGroup/FrameRate'))
    cursor = 0
    for item, (start, end) in zip(video_items, sources[0]['kept_frames']):
        clip = item_clip(item, objects).find('Clip')
        if (int(clip.findtext('InPoint', '0')), int(clip.findtext('OutPoint')),
                timing(item)) != (start * frame, end * frame, (cursor, cursor + (end-start)*frame)):
            raise ValueError('Tempos do projeto não correspondem ao plano editorial.')
        cursor += (end-start)*frame
    next_node = max(int(e.text) for e in root.findall('.//Node/ID')) + 1
    titles = editorial['titles'] if mode == 'simples' else [
        {'text': 'TESTE MOGRAPH', 'start': 0.0, 'end': 5.0}]
    created = []
    for i, title in enumerate(titles):
        if not (math.isfinite(title['start']) and math.isfinite(title['end']) and
                0 <= title['start'] < title['end'] <= duration/TICKS):
            raise ValueError('Intervalo de título inválido.')
        start = math.ceil(Fraction(str(title['start']))*TICKS/frame) * frame
        end = min(duration, math.floor(Fraction(str(title['end']))*TICKS/frame)*frame)
        if created and start < timing(created[-1])[1] or end <= start:
            raise ValueError('Títulos sobrepostos ou sem duração.')
        item = clone_graphic(root, templates[mode]['item'])
        set_text(item.find('.//Node'), 'ID', next_node + i)
        set_text(item.find('.//TrackItem'), 'Start', start)
        set_text(item.find('.//TrackItem'), 'End', end)
        clip = item_clip(item, object_index(root)).find('Clip')
        original_duration = int(clip.findtext('OutPoint')) - int(clip.findtext('InPoint'))
        if mode == 'mograph' and end-start != original_duration:
            raise ValueError('Teste MOGRT mantém exatamente a duração nativa de cinco segundos.')
        set_text(clip, 'OutPoint', int(clip.findtext('InPoint')) + end-start)
        text = title['text']
        if mode == 'simples':
            text = '\n'.join(textwrap.wrap(text, width=20, break_long_words=False, break_on_hyphens=False))
            if len(text.splitlines()) > 2:
                raise ValueError('Título precisa de mais de duas linhas; revisar texto.')
        update_text(root, item, text)
        created.append(item)
    new_track(root, created)
    for project in root.findall('Project'):
        if project.find('NextID') is not None:
            set_text(project, 'NextID', next_node + len(created))
    sequence = root.find('Sequence')
    old_name = sequence.findtext('Name')
    new_name = re.sub(r' - v\d+$', '', old_name) + f' - v{version:03}'
    for element in root.iter():
        if element.tag == 'Name' and element.text == old_name:
            element.text = new_name
    set_text(sequence.find('./Node/Properties'), 'Monitor.ProgramZoomOut', duration)
    set_text(sequence.find('./Node/Properties'), 'MZ.EditLine', timing(created[0])[0] if created else 0)
    for source in root.findall('VideoSequenceSource') + root.findall('AudioSequenceSource'):
        set_text(source, 'OriginalDuration', duration)
    # O MOGRT contém histórico de caminhos Mac; religa ao arquivo local existente.
    for media in root.findall('Media'):
        name = media.findtext('FilePath')
        if name and Path(name).suffix:
            if not Path(name).is_file():
                raise ValueError(f'Mídia necessária ausente: {name}')
            set_text(media, 'ActualMediaFilePath', name)
    if unchanged_payload(root) != baseline:
        raise ValueError('Áudio, legendas ou Lumetri modificados indevidamente.')
    # Originais e ajuste permanecem exatamente iguais, sem os dois exemplos no fim.
    for item in video_items + track_items(root.findall('VideoClipTrack')[1], objects):
        if ET.tostring(item) != ET.tostring(object_index(model)[item.get('ObjectID')]):
            raise ValueError('Cortes ou ajuste alterados indevidamente.')
    return root, {'mode': mode, 'templates': templates, 'titles': len(created),
                  'duration': duration/TICKS, 'native_editable': True,
                  'status': 'conferência estrutural; abertura e aparência no Premiere pendentes'}


def create(edition, model_path, mode):
    digest = file_hash(model_path)
    model = read_project(model_path)
    editorial_path = edition/'metadados'/'plano-editorial-pendente.json'
    editorial_digest = file_hash(editorial_path)
    editorial = json.loads(editorial_path.read_text(encoding='utf-8'))
    for source in editorial['sources']:
        if file_hash(Path(source['path'])) != source['sha256']:
            raise ValueError('Original alterado desde a análise.')
    version = next_version(edition)
    output = edition/'premiere'/f'timeline-v{version:03}.prproj'
    root, details = build(model, editorial, mode, version)
    # XML normaliza CR literal para LF; entidades preservam quebras nativas.
    xml = ET.tostring(root, encoding='utf-8', xml_declaration=True).replace(b'\r', b'&#13;')
    data = gzip.compress(xml, mtime=0)
    reread = ET.fromstring(gzip.decompress(data))
    if ET.tostring(reread) != ET.tostring(root):
        raise ValueError('Falha na releitura do projeto.')
    if file_hash(model_path) != digest or file_hash(editorial_path) != editorial_digest:
        raise ValueError('Projeto ou plano alterado durante a geração.')
    with output.open('xb') as stream:
        stream.write(data)
    record = {'version': version, 'base': str(model_path), 'base_sha256': digest,
              'project': str(output), 'project_sha256': file_hash(output),
              'editorial_sha256': editorial_digest, 'checks': details,
              'created_at': datetime.now().astimezone().isoformat(), 'api_calls': 0}
    with (edition/'metadados'/f'titulos-nativos-v{version:03}.json').open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    report(edition/'metadados',
           f'- Projeto: `../premiere/{output.name}`. Teste {mode}: {details["titles"]} título(s) editável(is) em V3.\n'
           '- V1 com cortes, V2 com ajuste, duas faixas de áudio e legendas preservados.\n'
           '- Exemplos fora do vídeo removidos somente da timeline desta cópia.\n'
           '- Texto substituído no buffer nativo, sem rasterização; estilos preservados.\n'
           '- No teste MOGRT, duração de 5 s, keyframes, efeitos, introdução/saída e imagem auxiliar mantidos.\n'
           '- Sem chamada à API: análise já armazenada foi reutilizada. Legendas ainda não são ocultadas sob títulos.\n'
           '- Piloto para esta edição. Conferência estrutural aprovada; abertura, aparência e animação dependem do teste no Premiere. Base preservada.',
           title=f'Títulos v{version:03}', section='## Títulos editáveis')
    print(f'v{version:03}: {output}\n{details["titles"]} título(s); conferir no Premiere.')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edition', required=True, type=Path)
    parser.add_argument('--model', required=True, type=Path)
    parser.add_argument('--mode', choices=('simples', 'mograph'), default='simples')
    args = parser.parse_args()
    create(args.edition.resolve(), args.model.resolve(), args.mode)
