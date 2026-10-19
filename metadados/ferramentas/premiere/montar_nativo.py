"""Monta novas timelines nativas a partir dos componentes do modelo preservado.

O modelo fornece classes/estilos; duração, mídias, cortes, texto e quantidade de
clipes vêm exclusivamente do trabalho atual. Não reutiliza o vídeo do modelo.
"""
from __future__ import annotations
import base64
import copy
from fractions import Fraction
import gzip
import math
import os
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

from gerar_prproj import (TICKS, read_project, object_index, uid_index, native_blobs,
                         track_items, item_clip, timing, set_text, unity_audio,
                         lumetri_values)
from titulos_nativos import inspect_templates, replace_text, update_text
from geometria_video import CANVAS, display_size, fit_plan
from cor_video import apply_color, color_plan
from normalizacao_audio import clip_audio_gains

SOURCE_TAGS = {'VideoMediaSource', 'AudioMediaSource', 'DataMediaSource'}


def global_elements(node):
    """Não interpreta referências dos objetos XML de escopo interno como globais."""
    yield node
    for child in node:
        if 'ObjectID' not in child.attrib and 'ObjectUID' not in child.attrib:
            yield from global_elements(child)


class Graph:
    def __init__(self, root):
        self.root = root
        self.objects = object_index(root)
        self.uids = uid_index(root)
        self.next_id = max(map(int, self.objects)) + 1
        self.next_node = max(int(e.text) for e in root.findall('.//Node/ID')) + 1

    def add(self, obj):
        if obj.get('ObjectID'):
            self.objects[obj.get('ObjectID')] = obj
        if obj.get('ObjectUID'):
            self.uids[obj.get('ObjectUID')] = obj
        self.root.append(obj)

    def clone(self, seed, media=False):
        owned = {}

        def visit(obj):
            key = obj.get('ObjectID') or obj.get('ObjectUID')
            if key in owned or (not media and obj.tag in SOURCE_TAGS):
                return
            owned[key] = obj
            for elem in global_elements(obj):
                ref, uref = elem.get('ObjectRef'), elem.get('ObjectURef')
                if ref:
                    visit(self.objects[ref])
                if uref and media:
                    visit(self.uids[uref])

        visit(seed)
        ids, uids = {}, {}
        for key, obj in owned.items():
            if obj.get('ObjectID'):
                ids[key] = str(self.next_id)
                self.next_id += 1
            else:
                uids[key] = str(uuid.uuid4())
        clones = {}
        for key, obj in owned.items():
            clone = copy.deepcopy(obj)
            if key in ids:
                clone.set('ObjectID', ids[key])
            else:
                clone.set('ObjectUID', uids[key])
            for elem in global_elements(clone):
                if elem.get('ObjectRef') in ids:
                    elem.set('ObjectRef', ids[elem.get('ObjectRef')])
                if elem.get('ObjectURef') in uids:
                    elem.set('ObjectURef', uids[elem.get('ObjectURef')])
                if elem.tag == 'ClipID':
                    elem.text = str(uuid.uuid4())
            for elem in clone.findall('.//Node/ID'):
                elem.text = str(self.next_node)
                self.next_node += 1
            if clone.tag == 'AudioClipTrackItem':
                set_text(clone, 'ID', uuid.uuid4())
            clones[key] = clone
            self.add(clone)
        return clones[seed.get('ObjectID') or seed.get('ObjectUID')], list(clones.values())


def set_refs(parent, path, tag, items, attr='ObjectRef'):
    container = parent.find(path)
    if container is None:
        raise ValueError(f'Container do modelo ausente: {path}')
    container.clear()
    container.set('Version', '1')
    for i, item in enumerate(items):
        key = item.get('ObjectID') if attr == 'ObjectRef' else item.get('ObjectUID')
        ET.SubElement(container, tag, Index=str(i), **{attr: key})


def layout(channels):
    return '[{"channellabel":0}]' if channels == 1 else '[{"channellabel":100},{"channellabel":101}]'


def audio_format(objects, channels, sample_rate):
    for obj in objects:
        for elem in obj.iter():
            if elem.tag == 'AudioChannelLayout':
                elem.text = layout(channels)
            if elem.tag == 'ChannelType':
                elem.text = '0' if channels == 1 else '1'
        if obj.tag == 'AudioFilterComponent':
            set_text(obj, 'FilterMatchName', 'Internal Volume Mono' if channels == 1 else 'Internal Volume Stereo')
            config = ('{"in":[{"layout":[0],"name":"Mono In","type":0}],"out":[{"layout":[0],"name":"Mono Out","type":0}]}'
                      if channels == 1 else
                      '{"in":[{"layout":[100,101],"name":"Stereo In","type":1}],"out":[{"layout":[100,101],"name":"Stereo Out","type":1}]}')
            set_text(obj, 'ChannelConfigData', config)
        for elem in obj.findall('.//AudioComponent/FrameRate'):
            elem.text = str(TICKS // sample_rate)


def bundle(graph, master, source, output_dir, color_options=None):
    new, owned = graph.clone(master, media=True)
    duration = round(source['duration'] * TICKS)
    media_frame = round(Fraction(TICKS, 1)/Fraction(source.get('media_fps', source['fps'])))
    for obj in owned:
        for elem in obj.findall('.//ClipName'):
            elem.text = source['name']
        for elem in obj.findall('.//MediaFrameRate'):
            elem.text = str(media_frame)
        if obj.tag == 'Media':
            for tag, value in [('FilePath', source['path']), ('ActualMediaFilePath', source['path']),
                               ('Title', source['name']), ('FileKey', uuid.uuid4()),
                               ('ContentAndMetadataState', uuid.uuid4())]:
                set_text(obj, tag, value)
            for old in list(obj.findall('RelativePath')):
                obj.remove(old)
            set_text(obj, 'RelativePath', os.path.relpath(source['path'], output_dir))
            for old in list(obj.findall('ModificationState')):
                obj.remove(old)
        if obj.tag in SOURCE_TAGS:
            set_text(obj, 'OriginalDuration', duration)
        if obj.tag in ('VideoClip', 'AudioClip'):
            cl = obj.find('Clip')
            set_text(cl, 'InPoint', 0)
            set_text(cl, 'OutPoint', duration)
        if obj.tag in ('VideoStream', 'AudioStream'):
            set_text(obj, 'Duration', duration)
            set_text(obj, 'FrameRate', TICKS // source['sample_rate'] if obj.tag == 'AudioStream'
                     else media_frame)
            for tag in ('PeakFilePath', 'ConformedAudioPath', 'OriginalColorSpace', 'OriginalImageOrientationType', 'CodecType', 'SampleType'):
                for elem in list(obj.findall(tag)):
                    obj.remove(elem)  # O importador lê o codec/perfil da nova mídia, não do modelo.
            if obj.tag == 'VideoStream':
                apply_color(obj, source, color_options)
                width, height = display_size(source)
                set_text(obj, 'FrameRect', f'0,0,{width},{height}')
    audio_format(owned, source['channels'], source['sample_rate'])
    # Uma única associação de canais por clipe, sem descartar o canal direito.
    group = graph.objects[new.find('AudioClipChannelGroups').get('ObjectRef')]
    refs = group.find('ClipChannelVectors')
    if refs is not None and len(refs):
        vectors = [graph.objects[e.get('ObjectRef')] for e in refs]
        channels = [graph.objects[e.get('ObjectRef')] for vector in vectors
                    for e in vector.findall('ClipChannels/ClipChannelItem')]
        set_refs(vectors[0], 'ClipChannels', 'ClipChannelItem', channels[:source['channels']])
        set_refs(group, 'ClipChannelVectors', 'ClipChannelVectorItem', vectors[:1])
    for obj in owned:
        if obj.tag == 'AudioClip':
            refs = obj.find('SecondaryContents')
            if refs is not None:
                for ref in list(refs)[source['channels']:]:
                    refs.remove(ref)
    chains = new.find('AudioComponentChains')
    if chains is not None:
        for ref in list(chains)[1:]:
            chains.remove(ref)
    set_text(new, 'Name', source['name'])
    return new, {obj.tag: obj for obj in owned if obj.tag in SOURCE_TAGS}, owned


def bin_entry(graph, prototype, master, name):
    entry = copy.deepcopy(prototype)
    entry.set('ObjectUID', str(uuid.uuid4()))
    entry.find('MasterClip').set('ObjectURef', master.get('ObjectUID'))
    set_text(entry.find('ProjectItem'), 'Name', name)
    node = entry.find('ProjectItem/Node')
    set_text(node, 'ID', graph.next_node)
    graph.next_node += 1
    graph.add(entry)
    return entry


def clip_timing(item, graph, start, end, source_in):
    set_text(item.find('.//TrackItem'), 'Start', start)
    set_text(item.find('.//TrackItem'), 'End', end)
    clip = item_clip(item, graph.objects)
    cl = clip.find('.//Clip')
    set_text(cl, 'InPoint', source_in)
    set_text(cl, 'OutPoint', source_in + end-start)
    return clip


def fit_motion(graph, item, prototype, source):
    """Escala nativa editável; orientação é lida da mídia, sem rotação duplicada."""
    plan = fit_plan(source)
    effect, owned = graph.clone(prototype)
    set_text(effect.find('Component'), 'ID', 1)
    params = {obj.findtext('ParameterID'): obj for obj in owned if obj.find('ParameterID') is not None}
    values = {'1': '0.5:0.5', '2': f'{plan["scale_percent"]:.12g}',
              '3': '100.', '4': 'true', '5': '0.', '6': '0.5:0.5'}
    for ident, value in values.items():
        param = params[ident]
        parts = param.findtext('StartKeyframe').split(',')
        parts[1] = value
        set_text(param, 'StartKeyframe', ','.join(parts))
        for tag in ('CurrentValue', 'Keyframes', 'IsTimeVarying'):
            elem = param.find(tag)
            if elem is not None:
                param.remove(elem)
    chain = graph.objects[item.find('.//Components').get('ObjectRef')]
    refs = chain.find('ComponentChain/Components')
    if refs is None:
        refs = ET.SubElement(chain.find('ComponentChain'), 'Components', Version='1')
    if any(graph.objects[r.get('ObjectRef')].findtext('MatchName') == 'AE.ADBE Motion' for r in refs):
        raise ValueError('O modelo já tem um Motion explícito; revisar antes de duplicá-lo.')
    # Como no projeto salvo pelo Premiere: Motion explícito substitui o default.
    for tag in ('DefaultMotion', 'DefaultMotionComponentID'):
        elem = chain.find(tag)
        if elem is not None:
            chain.remove(elem)
    ET.SubElement(refs, 'Component', Index=str(len(refs)), ObjectRef=effect.get('ObjectID'))
    return plan


def prune(root):
    # Materializa deduplicação antes de eliminar objetos não usados do modelo.
    blobs = native_blobs(root)
    for elem in root.iter():
        if elem.get('Encoding') == 'base64' and elem.get('BinaryHash'):
            elem.text = base64.b64encode(blobs[elem.get('BinaryHash')]).decode('ascii')
    objects, uids = object_index(root), uid_index(root)
    keep = set()

    def visit(obj):
        if id(obj) in keep:
            return
        keep.add(id(obj))
        for elem in global_elements(obj):
            if elem.get('ObjectRef'):
                visit(objects[elem.get('ObjectRef')])
            if elem.get('ObjectURef'):
                visit(uids[elem.get('ObjectURef')])

    for obj in root:
        if 'ObjectID' not in obj.attrib and 'ObjectUID' not in obj.attrib:
            visit(obj)
    for obj in list(root):
        if id(obj) not in keep:
            root.remove(obj)


def build(model_path, sources, cues, titles, output, animate=True, color_options=None):
    root = read_project(model_path)
    graph = Graph(root)
    original_objects, original_uids = dict(graph.objects), dict(graph.uids)
    templates = inspect_templates(root)
    tracks = root.findall('VideoClipTrack')
    audio_tracks = root.findall('AudioClipTrack')
    original_item = track_items(tracks[0], graph.objects)[0]
    audio_examples = [track_items(t, graph.objects)[0] for t in audio_tracks]
    adjustment = track_items(tracks[1], graph.objects)[0]
    caption_example = track_items(root.find('CaptionDataClipTrack'), graph.objects)[0]
    source_masters = [graph.uids[graph.objects[item.find('.//SubClip').get('ObjectRef')]
                       .find('MasterClip').get('ObjectURef')] for item in [original_item, audio_examples[1]]]
    frame = int(Fraction(TICKS, 1)/Fraction(sources[0]['fps']))
    motion_example = next(e for e in root.findall('VideoFilterComponent') if e.findtext('MatchName') == 'AE.ADBE Motion')
    if root.findtext('VideoTrackGroup/FrameRect') != f'0,0,{CANVAS[0]},{CANVAS[1]}':
        raise ValueError('O canvas do modelo não corresponde ao layout dos títulos e legendas.')
    for group in ('VideoTrackGroup', 'DataTrackGroup'):
        set_text(root.find(group+'/TrackGroup'), 'FrameRate', frame)
    root_items = root.find('RootProjectItem/ProjectItemContainer/Items')
    original_entries = root.findall('ClipProjectItem')
    def master_of(entry):
        return graph.uids[entry.find('MasterClip').get('ObjectURef')]

    def is_sequence(entry):
        for ref in master_of(entry).findall('Clips/Clip'):
            clip = graph.objects[ref.get('ObjectRef')]
            source_ref = clip.find('.//Clip/Source')
            if source_ref is not None and graph.objects[source_ref.get('ObjectRef')].tag == 'VideoSequenceSource':
                return True
        return False

    sequence_entry = next(e for e in original_entries if is_sequence(e))
    adjustment_entry = next(e for e in original_entries if master_of(e).findtext('IsAdjustmentLayer') == 'true')
    root_items.clear()
    root_items.set('Version', '1')
    entries = [sequence_entry, adjustment_entry]
    collections = [[], [], []]
    links = []
    cursor = 0
    for source in sources:
        source['clip_audio_gains'] = clip_audio_gains(source)
        new_original, original_sources, _ = bundle(graph, source_masters[0], source, output.parent, color_options)
        treated = {**source, **source['treated_audio']}
        new_audio, treated_sources, _ = bundle(graph, source_masters[1], treated, output.parent)
        entries += [bin_entry(graph, original_entries[1], new_original, source['name']),
                    bin_entry(graph, original_entries[2], new_audio, treated['name'])]
        for clip_index, (begin, end) in enumerate(source['kept_frames']):
            duration = (end-begin)*frame
            triple = []
            for index, prototype in enumerate([original_item, *audio_examples]):
                item, owned = graph.clone(prototype)
                clip = clip_timing(item, graph, cursor, cursor+duration, begin*frame)
                sub = graph.objects[item.find('.//SubClip').get('ObjectRef')]
                master = new_original if index < 2 else new_audio
                sub.find('MasterClip').set('ObjectURef', master.get('ObjectUID'))
                set_text(sub, 'Name', source['name'] if index < 2 else treated['name'])
                cl = clip.find('Clip')
                kind = 'VideoMediaSource' if index == 0 else 'AudioMediaSource'
                media_source = original_sources[kind] if index < 2 else treated_sources[kind]
                cl.find('Source').set('ObjectRef', media_source.get('ObjectID'))
                markers = cl.find('MarkerOwner')
                if markers is not None:
                    cl.remove(markers)
                if index == 0:
                    fit_motion(graph, item, motion_example, source)
                if index:
                    audio_format(owned, source['channels'], treated['sample_rate'] if index == 2 else source['sample_rate'])
                    refs = clip.find('SecondaryContents')
                    first_secondary = graph.objects[refs[0].get('ObjectRef')]
                    refs.clear()
                    refs.set('Version', '1')
                    for channel in range(source['channels']):
                        secondary, _ = graph.clone(first_secondary)
                        secondary.find('Content').set('ObjectRef', media_source.get('ObjectID'))
                        set_text(secondary, 'ChannelIndex', channel)
                        ET.SubElement(refs, 'SecondaryContentItem', Index=str(channel), ObjectRef=secondary.get('ObjectID'))
                    set_text(item.find('ClipTrackItem'), 'IsMuted', 'true' if index == 1 else 'false')
                    set_text(clip, 'Gain', 1.0)
                    if index == 2:
                        # Pico-alvo 0 dBFS; boost total limitado a +6 dB. Level = 0 dB.
                        set_text(clip, 'Gain', source['clip_audio_gains'][clip_index]['linear'])
                collections[index].append(item)
                triple.append(item)
            # Link só agrupa os clipes já criados; não duplicar o grafo antigo.
            link = copy.deepcopy(root.find('Link'))
            link.set('ObjectID', str(graph.next_id))
            graph.next_id += 1
            graph.add(link)
            set_refs(link, './TrackItemGroup/TrackItems', 'TrackItem', triple)
            links.append(link)
            cursor += duration
    set_refs(tracks[0], './/ClipItems/TrackItems', 'TrackItem', collections[0])
    for i, track in enumerate(audio_tracks):
        set_refs(track, './/ClipItems/TrackItems', 'TrackItem', collections[i+1])
    set_refs(root.find('Sequence'), './PersistentGroupContainer/LinkContainer/Links', 'Link', links)
    clip_timing(adjustment, graph, 0, cursor, int(item_clip(adjustment, graph.objects).findtext('Clip/InPoint')))

    # Um TranscriptClip e um Block independentes por legenda, texto nativo editável.
    caption_items = []
    for cue in cues:
        item, _ = graph.clone(caption_example)
        start = max(0, cue['start_ms']*TICKS//1000//frame*frame)
        end = min(cursor, max(start+frame, cue['end_ms']*TICKS//1000//frame*frame))
        if caption_items and start < timing(caption_items[-1])[1]:
            start = timing(caption_items[-1])[1]
        if end <= start:
            raise ValueError('Legenda sem espaço na timeline.')
        original_in = int(item_clip(item, graph.objects).findtext('.//Clip/InPoint'))
        clip_timing(item, graph, start, end, original_in)
        block = graph.objects[item.find('BlockVector/BlockVectorItem').get('ObjectRef')]
        value = block.find('FormattedTextData')
        blob = native_blobs(root)[value.get('BinaryHash')]
        value.text = base64.b64encode(replace_text(blob, cue['text'])).decode('ascii')
        value.set('BinaryHash', str(uuid.uuid4()))
        caption_items.append(item)
    set_refs(root.find('CaptionDataClipTrack'), './/ClipItems/TrackItems', 'TrackItem', caption_items)
    # O MasterClip sintético não deve apontar para uma legenda antiga do modelo.
    for item in caption_items:
        sub = graph.objects[item.find('.//SubClip').get('ObjectRef')]
        master, _ = graph.clone(original_uids[sub.find('MasterClip').get('ObjectURef')], media=True)
        sub.find('MasterClip').set('ObjectURef', master.get('ObjectUID'))

    # V3 fixo, independente do zoom do vídeo. Fade determinístico, sem chamada à IA.
    title_track = copy.deepcopy(tracks[0])
    title_track.set('ObjectUID', str(uuid.uuid4()))
    set_text(title_track.find('ClipTrack/Track'), 'Index', 2)
    set_text(title_track.find('ClipTrack/Track'), 'ID', 3)
    set_text(title_track.find('ClipTrack/ClipItems'), 'Index', 2)
    set_text(title_track.find('ClipTrack/TransitionItems'), 'Index', 2)
    title_items = []
    for title in titles:
        start = math.ceil(Fraction(str(title['start']))*TICKS/frame)*frame
        end = min(cursor, math.floor(Fraction(str(title['end']))*TICKS/frame)*frame)
        if end <= start:
            continue
        item, _ = graph.clone(original_objects[templates['simples']['item']])
        source_in = int(item_clip(item, graph.objects).findtext('Clip/InPoint'))
        clip_timing(item, graph, start, end, source_in)
        text = title['text']
        if len(text) > 20 and len(text.split()) > 1:
            words = text.split()
            split = min(range(1, len(words)), key=lambda i: max(len(' '.join(words[:i])), len(' '.join(words[i:]))))
            text = ' '.join(words[:split])+'\n'+' '.join(words[split:])
        update_text(root, item, text)
        if animate:
            chain = graph.objects[item.find('.//Components').get('ObjectRef')]
            geometry = next(e for e in root.findall('VideoFilterComponent') if e.findtext('MatchName') == 'AE.ADBE Geometry')
            effect, owned = graph.clone(geometry)
            opacity = next(e for e in owned if e.findtext('Name') == 'Opacity')
            fade = min(round(.12*TICKS), (end-start)//4)
            set_text(opacity, 'StartKeyframe', '-91445760000000000,0.,0,0,0,0,0,0')
            points = [(source_in, 0), (source_in+fade, 100),
                      (source_in+end-start-fade, 100), (source_in+end-start, 0)]
            set_text(opacity, 'Keyframes', ''.join(f'{t},{v}.,0,0,0,0,0,0;' for t, v in points))
            set_text(opacity, 'IsTimeVarying', 'true')
            refs = chain.find('ComponentChain/Components')
            ET.SubElement(refs, 'Component', Index=str(len(refs)), ObjectRef=effect.get('ObjectID'))
        title_items.append(item)
    set_refs(title_track, './/ClipItems/TrackItems', 'TrackItem', title_items)
    graph.add(title_track)
    group = root.find('VideoTrackGroup/TrackGroup')
    ET.SubElement(group.find('Tracks'), 'Track', Index='2', ObjectURef=title_track.get('ObjectUID'))
    set_text(group, 'NextTrackID', 4)
    for i, entry in enumerate(entries):
        ET.SubElement(root_items, 'Item', Index=str(i), ObjectURef=entry.get('ObjectUID'))
    sequence = root.find('Sequence')
    set_text(sequence, 'Name', output.stem)
    set_text(sequence_entry.find('ProjectItem'), 'Name', output.stem)
    master_sequence = graph.uids[sequence_entry.find('MasterClip').get('ObjectURef')]
    set_text(master_sequence, 'Name', output.stem)
    for tag in ('VideoSequenceSource', 'AudioSequenceSource'):
        for obj in root.findall(tag):
            set_text(obj, 'OriginalDuration', cursor)
    props = sequence.find('Node/Properties')
    for name, value in [('Monitor.ProgramZoomIn', 0), ('Monitor.ProgramZoomOut', cursor), ('MZ.EditLine', 0)]:
        set_text(props, name, value)
    project = root.find('Project[@ObjectID]')
    set_text(project, 'NextID', graph.next_node)
    props = project.find('Node/Properties')
    set_text(props, 'project.settings.lastknowngoodprojectpath', output)
    set_text(props, 'project.settings.lastknownparentdirectorypathaboveprojectpath', output.parent)
    set_text(props, 'MZ.Project.GUID', uuid.uuid4())
    if sources[0]['channels'] == 2:
        # A2 recebe os dois canais na mesma faixa; não usa somente o canal esquerdo.
        audio_format(list(root), 2, 48000)
    unity_audio(root)
    prune(root)
    # O gerador não pode carregar nenhum caminho legado de mídia ao abrir.
    for media in root.findall('Media'):
        name = media.findtext('FilePath')
        if name and Path(name).suffix and not Path(name).is_file():
            raise ValueError(f'Mídia ausente no novo projeto: {name}')
    details = {'clips': len(collections[0]), 'captions': len(caption_items),
               'titles': len(title_items), 'duration_seconds': cursor/TICKS,
               'geometry': [{'source': s['name'], **fit_plan(s)} for s in sources],
               'color': [color_plan(s, color_options) for s in sources],
               'tracks': {'video': 3, 'audio': 2, 'captions': 1}, 'lumetri': lumetri_values(root)}
    xml = ET.tostring(root, encoding='utf-8', xml_declaration=True).replace(b'\r', b'&#13;')
    data = gzip.compress(xml, mtime=0)
    reread = ET.fromstring(gzip.decompress(data))
    if ET.tostring(reread) != ET.tostring(root):
        raise ValueError('Falha de releitura do projeto.')
    return data, details
