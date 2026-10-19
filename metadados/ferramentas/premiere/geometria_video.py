"""Geometria de exibição da mídia, separada do canvas dos gráficos."""
import math

CANVAS = (1080, 1920)


def normalized_rotation(value):
    rotation = float(value)
    if not math.isfinite(rotation):
        raise ValueError('Rotação inválida no vídeo.')
    quarter = round(rotation/90)
    if abs(rotation-quarter*90) > .01:
        raise ValueError('Rotação diferente de múltiplos de 90° exige revisão; original preservado.')
    return ((quarter*90+180) % 360)-180


def display_size(source):
    width, height = int(source['width']), int(source['height'])
    if min(width, height) <= 0:
        raise ValueError('Dimensões inválidas no vídeo.')
    rotation = normalized_rotation(source.get('rotation_degrees', 0))
    return (height, width) if abs(rotation) == 90 else (width, height)


def fit_plan(source, canvas=CANVAS):
    width, height = display_size(source)
    target_width, target_height = canvas
    scale = min(target_width/width, target_height/height)*100
    return {'encoded_size': [int(source['width']), int(source['height'])],
            'display_size': [width, height], 'canvas': list(canvas),
            'source_rotation_degrees': normalized_rotation(source.get('rotation_degrees', 0)),
            'orientation': 'metadados do original interpretados pelo importador',
            'scale_percent': scale, 'center_normalized': [.5, .5],
            'additional_rotation_degrees': 0,
            'mode': 'encaixar sem recorte', 'video_reencoded': False}
