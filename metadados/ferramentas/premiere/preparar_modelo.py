"""Preserva os componentes do editor sem dependência das mídias do cliente."""
import gzip
from pathlib import Path
import xml.etree.ElementTree as ET
from gerar_prproj import read_project, native_blobs, reference_graph
from exportar_premiere import CENTRAL


def prepare():
    folder = CENTRAL/'prproj'
    output = folder/'modelo-base.prproj'
    if output.exists():
        raise ValueError('Modelo-base já existe; não sobrescrever sem revisão.')
    root = read_project(folder/'referencia.prproj')
    for media in root.findall('Media'):
        path = media.findtext('FilePath', '')
        if not Path(path).suffix:
            continue
        if Path(path).suffix.lower() == '.png':
            relative = media.findtext('RelativePath').lstrip('.\\/')
            value = str((folder/relative).resolve())
            if not Path(value).is_file():
                raise ValueError('Imagem auxiliar do MOGRT não foi preservada.')
        elif Path(path).suffix.lower() in ('.mov', '.mp4'):
            value = '@ORIGINAL@'
        elif Path(path).suffix.lower() == '.wav':
            value = '@AUDIO@'
        elif Path(path).suffix.lower() == '.srt':
            value = '@LEGENDAS@'
        else:
            raise ValueError(f'Mídia não reconhecida no modelo: {Path(path).name}')
        for tag in ('FilePath', 'ActualMediaFilePath'):
            elem = media.find(tag)
            if elem is not None:
                elem.text = value
        for tag in ('RelativePath', 'MediaFileHistory0', 'MediaFileHistory1'):
            for elem in list(media.findall(tag)):
                media.remove(elem)
    for obj in root:
        for tag in ('PeakFilePath', 'ConformedAudioPath'):
            for elem in list(obj.findall(tag)):
                obj.remove(elem)
    props = root.find('Project[@ObjectID]/Node/Properties')
    props.find('project.settings.lastknowngoodprojectpath').text = str(output)
    props.find('project.settings.lastknownparentdirectorypathaboveprojectpath').text = str(folder)
    data = ET.tostring(root, encoding='utf-8', xml_declaration=True).replace(b'\r', b'&#13;')
    with output.open('xb') as stream:
        stream.write(gzip.compress(data, mtime=0))
    print('Modelo-base independente preservado.')


def relocate_references(previous_folder):
    """Atualiza caminhos dos modelos após reorganização, sem alterar componentes."""
    folder = (CENTRAL/'prproj').resolve()
    previous_folder = previous_folder.resolve()
    changes = 0
    for path in folder.glob('*.prproj'):
        root = read_project(path)
        graph, blobs = reference_graph(root), native_blobs(root)
        changed = False
        for element in root.iter():
            if not element.text:
                continue
            text = element.text.replace(str(previous_folder), str(folder))
            text = text.replace(previous_folder.as_posix(), folder.as_posix())
            if text != element.text:
                element.text = text
                changed = True
        # O snapshot de estudo também acompanha a imagem auxiliar preservada.
        for media in root.findall('Media'):
            name = media.findtext('FilePath', '')
            if Path(name).suffix.lower() != '.png':
                continue
            matches = list((folder/'Motion Graphics Template Media').rglob(Path(name).name))
            if len(matches) != 1:
                raise ValueError('Imagem auxiliar ausente ou ambígua na nova pasta.')
            for tag in ('FilePath', 'ActualMediaFilePath'):
                element = media.find(tag)
                if element is not None and element.text != str(matches[0]):
                    element.text = str(matches[0])
                    changed = True
        if not changed:
            continue
        xml = ET.tostring(root, encoding='utf-8', xml_declaration=True).replace(b'\r', b'&#13;')
        checked = ET.fromstring(xml)
        if reference_graph(checked) != graph or native_blobs(checked) != blobs:
            raise ValueError('Relocalização alterou a estrutura ou os estilos do modelo.')
        temporary = path.with_suffix('.prproj.relocalizando')
        temporary.write_bytes(gzip.compress(xml, mtime=0))
        temporary.replace(path)
        changes += 1
    print(f'Modelos com caminhos internos atualizados: {changes}.')


if __name__ == '__main__':
    prepare()
