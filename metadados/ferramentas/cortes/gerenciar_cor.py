"""Politica explicita de cor; nao confunde etiqueta Rec.709 com conversao real."""

HDR_TRANSFERS = {'arib-std-b67': 'Rec.2100 HLG', 'smpte2084': 'Rec.2100 PQ'}
FIELDS = ('color_primaries', 'color_transfer', 'color_space', 'color_range', 'pix_fmt')
OUTPUT_ARGS = ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv']


def color_plan(stream):
    tags = {key: stream.get(key, 'unknown') for key in FIELDS}
    transfer = tags['color_transfer']
    primaries = tags['color_primaries']
    matrix = tags['color_space']
    dovi = next((side for side in stream.get('side_data_list', [])
                 if 'dovi' in side.get('side_data_type', '').lower()), None)
    profile = int(dovi.get('dv_profile', -1)) if dovi else None
    hdr = transfer in HDR_TRANSFERS or dovi is not None
    if dovi and profile not in (5, 8):
        raise ValueError(f'Dolby Vision perfil {profile} exige tratamento especifico. Conversao automatica interrompida.')
    if transfer in HDR_TRANSFERS and primaries != 'bt2020':
        raise ValueError(f'Metadados HDR contraditorios: {transfer} com primarias {primaries}. Conferir o original.')
    if profile != 5:
        if primaries not in {'bt709', 'bt2020', 'smpte170m', 'bt470bg', 'smpte240m'} or transfer not in {
            'bt709', 'arib-std-b67', 'smpte2084', 'bt2020-10', 'bt2020-12',
            'smpte170m', 'gamma22', 'gamma28', 'iec61966-2-1', 'smpte240m',
        } or matrix in {'unknown', 'unspecified', 'reserved'} or tags['color_range'] not in {'tv', 'pc'}:
            raise ValueError('Cor inicial ausente ou nao suportada: ' + str(tags) + '. Conferir metadados antes de converter.')
    source = HDR_TRANSFERS.get(transfer, 'Rec.709 SDR' if primaries == transfer == matrix == 'bt709'
                               else f'SDR {primaries}/{transfer}/{matrix}')
    if dovi:
        source = f'Dolby Vision perfil {profile} ({"IPT-PQ" if profile == 5 else source})'
    same = not hdr and primaries == transfer == matrix == 'bt709'
    plan = {'source_label': source, 'source_tags': tags, 'dolby_vision': dovi,
            'destination_label': 'Rec.709 SDR / faixa limitada / 8 bits',
            'destination_tags': {'color_primaries': 'bt709', 'color_transfer': 'bt709',
                                 'color_space': 'bt709', 'color_range': 'tv'},
            'converted': not same or tags['color_range'] != 'tv',
            'hdr_to_sdr': hdr, 'ffmpeg_input_args': [], 'filter': 'format=yuv420p',
            'care': ['Rec.709 SDR mantido; sem tone mapping. Ajustes basais, se habilitados, são aplicados depois.']}
    if same:
        if tags['color_range'] == 'pc':
            plan.update(filter='scale=in_range=full:out_range=limited,format=yuv420p',
                        care=['Faixa completa convertida para limitada; primárias/curva Rec.709 mantidas. Sem tone mapping.'])
        return plan
    conversion = ('libplacebo=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv:'
                  'format=yuv420p:apply_dolbyvision=true:gamut_mode=perceptual:peak_detect=true:'
                  'dithering=blue:brightness=0:contrast=1:saturation=1:contrast_recovery=0:'
                  + ('tonemapping=bt.2390' if hdr else 'tonemapping=clip'))
    # Remover apenas metadados HDR depois de transformar pixels, sem apagar outros side data.
    cleanup = ','.join('sidedata=mode=delete:type=' + kind for kind in (
        'MASTERING_DISPLAY_METADATA', 'CONTENT_LIGHT_LEVEL', 'DYNAMIC_HDR_PLUS',
        'DOVI_RPU_BUFFER', 'DOVI_METADATA', 'DYNAMIC_HDR_VIVID'))
    care = ['Conversão real de primárias/curva/matriz para Rec.709; saída explicitamente identificada.',
            'Mapeamento perceptual de gamut para acomodar cores fora de Rec.709.',
            'Na conversão técnica, antes do basal: saturação/contraste neutros (1), brilho neutro (0), sem LUT.',
            'Dithering blue-noise na saída de 8 bits para atenuar banding.']
    if hdr:
        care.insert(0, 'Tone mapping BT.2390 e detecção dinâmica de pico: compressão de altas luzes HDR para SDR.')
        care.append('Metadados HDR removidos após conversão; faixa dinâmica HDR não é preservada na cópia SDR.')
    if dovi:
        care.append('Processamento Dolby Vision habilitado no libplacebo; requer metadados válidos no arquivo.')
    plan.update(filter=conversion + ',' + cleanup, care=care,
                ffmpeg_input_args=['-init_hw_device', 'vulkan=cor', '-filter_hw_device', 'cor'])
    return plan


def color_description(plan):
    return (f'Cor inicial: {plan["source_label"]}\nCor de saida: {plan["destination_label"]}'
            f'\nConversao: {"SIM" if plan["converted"] else "NAO (SDR mantido)"}\n'
            + '\n'.join('- ' + item for item in plan['care']))


def validate_output(stream):
    for key, expected in {'color_primaries': 'bt709', 'color_transfer': 'bt709',
                          'color_space': 'bt709', 'color_range': 'tv'}.items():
        if stream.get(key) != expected:
            raise ValueError(f'Cor da saida nao confere: {key}={stream.get(key)}; esperado {expected}.')
