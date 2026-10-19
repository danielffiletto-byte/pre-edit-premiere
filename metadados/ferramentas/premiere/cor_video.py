"""Interpretação nativa observada no projeto aprovado; não converte pixels."""
import json
import xml.etree.ElementTree as ET

# Copiado do OverriddenColorSpace salvo pelo Premiere 2026 no estudo Andrea.
REC709 = {"baseColorProfile": {"colorProfileData": "AQAAAP////8=",
                              "colorProfileName": "BT.709,10-bit,Display-Referred"},
          "baseProfileType": 1}
NON709 = {
    'color_primaries': {'bt2020', 'bt470m', 'bt470bg', 'smpte170m', 'smpte240m',
                        'film', 'smpte428', 'smpte431', 'smpte432', 'jedec-p22'},
    'color_transfer': {'arib-std-b67', 'smpte2084', 'bt2020-10', 'bt2020-12',
                       'smpte428', 'smpte240m', 'gamma22', 'gamma28', 'linear',
                       'iec61966-2-1', 'iec61966-2-4', 'bt1361e', 'log', 'log_sqrt'},
    'color_space': {'bt2020nc', 'bt2020c', 'bt470bg', 'smpte170m', 'smpte240m',
                    'fcc', 'ycgco', 'ictcp', 'chroma-derived-nc', 'chroma-derived-c'},
}


def color_plan(source, options=None):
    color = source.get('color', {})
    known_non709 = any(str(color.get(k, '')).lower() in values for k, values in NON709.items())
    enabled = (options or {}).get('override_rec709', True)
    applied = enabled and known_non709
    if applied:
        status = 'Override Rec.709 no Premiere (interpretação; sem tone mapping ou conversão do original)'
    elif not enabled:
        status = 'Interpretação automática; override desativado na configuração'
    elif color.get('color_primaries') == 'bt709' and color.get('color_transfer') == 'bt709':
        status = 'Rec.709 identificado; sem override'
    else:
        status = 'Perfil não conclusivo; interpretação automática, conferir no Premiere'
    return {'source': source.get('name', ''), 'original': color, 'override_applied': applied,
            'status': status, 'video_reencoded': False, 'tone_mapping': False}


def apply_color(stream, source, options=None):
    # Nunca herdar a interpretação da mídia-modelo para um novo bruto.
    for tag in ('OverriddenColorSpace', 'IsColorSpacOverridden'):
        for elem in list(stream.findall(tag)):
            stream.remove(elem)
    plan = color_plan(source, options)
    if plan['override_applied']:
        ET.SubElement(stream, 'OverriddenColorSpace').text = json.dumps(REC709, separators=(',', ':'))
        # A grafia "Spac" é a usada pelo próprio Premiere, não corrigir.
        ET.SubElement(stream, 'IsColorSpacOverridden').text = 'true'
    return plan
