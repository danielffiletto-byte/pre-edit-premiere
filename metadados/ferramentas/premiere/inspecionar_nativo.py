"""Inspeção estrutural de projeto-modelo, sem modificar o arquivo."""
import argparse
from collections import Counter
import gzip
from pathlib import Path
import xml.etree.ElementTree as ET


def compact(element, limit=3500):
    clone = ET.fromstring(ET.tostring(element))
    for item in clone.iter():
        if item.text and len(item.text) > 250:
            item.text = f'[texto/blob de {len(item.text)} caracteres]'
    return ET.tostring(clone, encoding='unicode')[:limit]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project', type=Path)
    parser.add_argument('--ids', nargs='*', default=[])
    parser.add_argument('--tags', nargs='*', default=[])
    parser.add_argument('--per-tag', type=int, default=2)
    parser.add_argument('--xpath', nargs='*', default=[])
    parser.add_argument('--timeline', action='store_true')
    parser.add_argument('--blobs', nargs='*', default=[])
    parser.add_argument('--audio', action='store_true')
    args = parser.parse_args()
    root = ET.fromstring(gzip.decompress(args.project.read_bytes()))
    print('ROOT', root.tag, root.attrib)
    print('COUNTS', Counter(child.tag for child in root))
    shown = Counter()
    for child in root:
        if (child.attrib.get('ObjectID') in args.ids or
                child.attrib.get('ObjectUID') in args.ids or
                (child.tag in args.tags and shown[child.tag] < args.per_tag)):
            print(compact(child))
            shown[child.tag] += 1
    for xpath in args.xpath:
        for child in root.findall(xpath):
            print(compact(child))
    objects = {c.attrib['ObjectID']: c for c in root if 'ObjectID' in c.attrib}
    if args.timeline:
        for track in root:
            if track.tag not in ('VideoClipTrack', 'AudioClipTrack', 'CaptionDataClipTrack'):
                continue
            refs = track.findall('.//ClipItems/TrackItems/TrackItem')
            print('TRACK', track.tag, track.attrib, 'items', len(refs))
            for ref in refs[:2] + refs[-2:]:
                item = objects[ref.attrib['ObjectRef']]
                timing = item.find('.//TrackItem')
                subref = item.find('.//SubClip')
                sub = objects[subref.attrib['ObjectRef']]
                clip = objects[sub.find('Clip').attrib['ObjectRef']]
                print('ITEM', item.attrib, 'timeline', [(c.tag, c.text) for c in timing],
                      'sub', sub.attrib, 'name', sub.findtext('Name'),
                      'source', compact(clip, 900))
    if args.blobs:
        import base64
        for ident in args.blobs:
            obj = objects[ident]
            for elem in obj.iter():
                if elem.attrib.get('Encoding') == 'base64':
                    data = base64.b64decode(elem.text or '')
                    print('BLOB', ident, elem.tag, len(data), repr(data[:1100]))
    if args.audio:
        counts = Counter((p.findtext('Name'), p.findtext('CurrentValue'), p.findtext('StartKeyframe'))
                         for p in root.findall('AudioComponentParam')
                         if p.findtext('Name') in ('Level', 'Volume'))
        print('AUDIO PARAMETERS', counts)
        for track in root.findall('AudioClipTrack'):
            refs = track.findall('.//ClipItems/TrackItems/TrackItem')
            print('AUDIO TRACK', track.attrib.get('ObjectUID'), 'index', track.findtext('.//Track/Index'), 'items', len(refs))
            for ref in refs[:1]:
                item = objects[ref.attrib['ObjectRef']]
                sub = objects[item.find('.//SubClip').attrib['ObjectRef']]
                clip = objects[sub.find('Clip').attrib['ObjectRef']]
                chain = objects[item.find('.//Components').attrib['ObjectRef']]
                print('AUDIO ITEM', compact(item, 1200), 'CLIP', compact(clip, 1500))
                for component_ref in chain.findall('.//Components/Component'):
                    component = objects[component_ref.attrib['ObjectRef']]
                    for param_ref in component.findall('.//Params/Param'):
                        print('PARAM', compact(objects[param_ref.attrib['ObjectRef']], 900))
        for elem in root.iter():
            if 'gain' in elem.tag.lower():
                print('GAIN', compact(elem, 700))


if __name__ == '__main__':
    main()
