"""Revisa cópia do projeto corrigido pelo editor, mantendo somente duas faixas.

Não reconstrói vídeo/legendas/Lumetri, nem altera os arquivos recebidos. O piloto
normaliza pico amostral por trecho em AudioClip/Gain, deixando Level em 0 dB.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import gzip
import json
import math
from pathlib import Path
import re
import sys
import wave
import xml.etree.ElementTree as ET

from gerar_prproj import (TICKS, SECTION, DEFAULT_CLIENTS, read_project, object_index,
                          uid_index, track_items, item_clip, clip_media, timing,
                          set_text, native_blobs, lumetri_values, unity_audio,
                          next_version, file_hash, report)
from console_pipeline import LiveProgress, summary


def clip_gain(path, clip, objects):
    """Pico do canal efetivamente usado pelo editor, no intervalo da mídia fonte."""
    refs = clip.findall('./SecondaryContents/SecondaryContentItem')
    if len(refs) != 1:
        raise ValueError('Este piloto de revisão exige clipe mono com um canal fonte.')
    channel = int(objects[refs[0].attrib['ObjectRef']].findtext('ChannelIndex', '0'))
    start = int(clip.findtext('./Clip/InPoint', '0'))
    end = int(clip.findtext('./Clip/OutPoint'))
    with wave.open(str(path), 'rb') as stream:
        width, channels, rate = stream.getsampwidth(), stream.getnchannels(), stream.getframerate()
        if width != 3 or not 0 <= channel < channels:
            raise ValueError('Esperado WAV PCM 24-bit com canal fonte válido.')
        first = round(start * rate / TICKS)
        last = round(end * rate / TICKS)
        if not 0 <= first < last <= stream.getnframes():
            raise ValueError('Limites de clipe fora do WAV tratado.')
        stream.setpos(first)
        pcm = stream.readframes(last - first)
    peak = max(abs(int.from_bytes(pcm[i:i+width], 'little', signed=True))
               for i in range(channel * width, len(pcm), channels * width))
    if peak == 0:
        raise ValueError('Trecho tratado totalmente mudo; normalização recusada.')
    return (1 << (width * 8 - 1)) / peak


def media_tracks(root):
    objects, uids = object_index(root), uid_index(root)
    original, treated = [], []
    for track in root.findall('AudioClipTrack'):
        items = track_items(track, objects)
        if not items:
            continue
        media = clip_media(item_clip(items[0], objects), objects, uids)
        path = Path(media.findtext('FilePath'))
        if path.suffix.lower() == '.wav':
            treated.append(track)
        elif path.suffix.lower() in ('.mov', '.mp4'):
            original.append(track)
        else:
            raise ValueError(f'Tipo de áudio inesperado: {path}')
    if not original or len(treated) != 1:
        raise ValueError('Esperado projeto revisado com original e exatamente uma faixa tratada preenchida.')
    original.sort(key=lambda t: int(t.findtext('.//Track/Index')))
    return original[0], treated[0]


def adapt(reviewed, version):
    root = copy.deepcopy(reviewed)
    before_lumetri, before_blobs = lumetri_values(root), native_blobs(root)
    objects, uids = object_index(root), uid_index(root)
    original, treated = media_tracks(root)
    keep = [original, treated]
    discarded = [track for track in root.findall('AudioClipTrack') if track not in keep]
    removed_uids = {t.attrib['ObjectUID'] for t in discarded}
    removed_ids = {item.attrib['ObjectID'] for t in discarded for item in track_items(t, objects)}
    # Retira ligações aos clipes descartados; vídeo, backup e tratado ficam vinculados.
    for link in root.findall('Link'):
        container = link.find('./TrackItemGroup/TrackItems')
        for child in list(container):
            if child.attrib.get('ObjectRef') in removed_ids:
                container.remove(child)
        for index, child in enumerate(container):
            child.set('Index', str(index))
    for item in list(root):
        if item in discarded or (item.tag == 'AudioClipTrackItem' and item.attrib.get('ObjectID') in removed_ids):
            root.remove(item)
    # As fontes do mixer precisam acompanhar as duas faixas, não só a timeline.
    for container in ([root.find('./AudioTrackGroup/TrackGroup/Tracks')] +
                      root.findall('./AudioTrackInlet/Sources')):
        for child in list(container):
            if child.attrib.get('ObjectURef') in removed_uids:
                container.remove(child)
        ordered = sorted(container, key=lambda c: keep.index(uids[c.attrib['ObjectURef']]))
        container[:] = ordered
        for index, child in enumerate(container):
            child.set('Index', str(index))
    gains = []
    for index, track in enumerate(keep):
        for path in ('./ClipTrack/Track/Index', './ClipTrack/ClipItems/Index', './ClipTrack/TransitionItems/Index'):
            element = track.find(path)
            if element is not None:
                element.text = str(index)
        for item in track_items(track, objects):
            set_text(item.find('ClipTrackItem'), 'IsMuted', 'true' if index == 0 else 'false')
            if index == 1:
                clip = item_clip(item, objects)
                media = clip_media(clip, objects, uids)
                gain = clip_gain(Path(media.findtext('FilePath')), clip, objects)
                saved = clip.findtext('Gain')
                if saved is None or abs(20 * math.log10(gain / float(saved))) > .01:
                    raise ValueError('Normalização calculada diverge da correção salva pelo editor; revisar antes de exportar.')
                # Preserva os números exatos que o próprio Premiere escreveu.
                gains.append({'gain': float(saved), 'gain_db': 20 * math.log10(float(saved)),
                              'computed_gain': gain})
    unity_audio(root)
    sequence = root.find('Sequence')
    old_name = sequence.findtext('Name')
    new_name = re.sub(r' - v\d+$', '', old_name) + f' - v{version:03}'
    for element in root.iter():
        if element.tag == 'Name' and element.text == old_name:
            element.text = new_name
        if (element.attrib.get('ObjectRef') in removed_ids or
                element.attrib.get('ObjectURef') in removed_uids):
            raise ValueError('Sobrou referência a clipe/faixa removidos.')
    # Preserva os blocos de texto, não apenas seu conteúdo legível.
    if lumetri_values(root) != before_lumetri or native_blobs(root) != before_blobs:
        raise ValueError('Lumetri ou blocos binários mudaram durante a revisão.')
    for tag in ('VideoClipTrackItem', 'CaptionDataClipTrackItem'):
        if [ET.tostring(x) for x in root.findall(tag)] != [ET.tostring(x) for x in reviewed.findall(tag)]:
            raise ValueError('Vídeo ou legendas alterados indevidamente.')
    return root, gains


def create(edition, reviewed_path):
    reviewed_hash = file_hash(reviewed_path)
    reviewed = read_project(reviewed_path)
    version = next_version(edition)
    output = edition / 'premiere' / f'timeline-v{version:03}.prproj'
    # Todos os caminhos reutilizados são verificados antes de salvar a nova cópia.
    media_checks = []
    for media in reviewed.findall('Media'):
        name = media.findtext('FilePath')
        if name and Path(name).suffix:
            path = Path(name)
            if not path.is_file():
                raise ValueError(f'Mídia ausente: {path}')
            media_checks.append({'path': str(path), 'sha256': file_hash(path)})
    with LiveProgress('Conferindo faixas e normalização por trecho', inline=True):
        root, gains = adapt(reviewed, version)
    with LiveProgress('Salvando projeto revisado', inline=True):
        compressed = gzip.compress(ET.tostring(root, encoding='utf-8', xml_declaration=True), mtime=0)
        parsed = ET.fromstring(gzip.decompress(compressed))
        if len(parsed.findall('AudioClipTrack')) != 2:
            raise ValueError('Projeto não ficou com exatamente duas faixas de áudio.')
        if file_hash(reviewed_path) != reviewed_hash:
            raise ValueError('Projeto editado mudou durante o processamento.')
        with output.open('xb') as stream:
            stream.write(compressed)
        record = {'version': version, 'created_at': datetime.now().astimezone().isoformat(),
                  'base_reviewed': str(reviewed_path), 'base_sha256': reviewed_hash,
                  'output': str(output), 'sha256': file_hash(output), 'media': media_checks,
                  'audio_gain_per_clip': gains,
                  'clip_level': 'padrão nativo de 0 dB, conforme projeto corrigido pelo editor',
                  'tracks': ['original desativado', 'tratado ativo'],
                  'status': 'testes estruturais aprovados; reprodução no Premiere pendente'}
        metadata = edition / 'metadados'
        with (metadata / f'plano-nativo-v{version:03}.json').open('x', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
    report(metadata,
           f'- Status: PRPROJ v{version:03} GERADO; conferir no Premiere.\n'
           f'- Projeto: `../premiere/{output.name}`; base: cópia v003 corrigida pelo editor.\n'
           '- Apenas duas faixas: A1 original desativado; A2 tratado ativo. Não há faixas vazias ou duplicadas. Mantido o canal mono usado pelo editor, sem remisturar o WAV estéreo.\n'
           '- Level: padrão nativo de 0 dB; corrigida a interpretação errada que havia gerado +15 dB. Não usar CurrentValue=1 como se fosse unity nesse controle.\n'
           f'- Audio Gain: normalização individual dos {len(gains)} trechos para pico amostral 0 dBFS. Ganhos exatos salvos pelo Premiere foram preservados e conferidos contra o WAV (diferença máxima aceita: 0,01 dB).\n'
           '- Cortes, 32 legendas nativas de até duas linhas explícitas e camada de ajuste/Lumetri preservados.\n'
           '- Reutiliza o WAV/SRT v003 para economizar processamento; não foram gerados novos arquivos de mídia ou XML.\n'
           '- Cópia do editor v003, modelo e mídias preservados. Gerador segue piloto, ainda não é montagem genérica para outros vídeos.\n'
           '- Próxima etapa após aprovação: definir integração da LLM para títulos; nenhuma chave/API foi usada nesta revisão.',
           title=f'Revisão de áudio v{version:03}', section=SECTION)
    summary('Projeto revisado pronto', [str(output), f'Duas faixas | {len(gains)} ganhos de pico conferidos',
                                       'Level 0 dB | Lumetri e legendas preservados'])
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edition', type=Path, required=True)
    parser.add_argument('--reviewed', type=Path, required=True)
    args = parser.parse_args()
    try:
        create(args.edition.resolve(), args.reviewed.resolve())
    except Exception as error:
        print(f'ERRO: {error}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
