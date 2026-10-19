"""Normaliza pico, detecta silencio e encurta pausas. Python padrao + FFmpeg."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from gerenciar_cor import OUTPUT_ARGS, color_description, color_plan, validate_output
from historico_edicao import (is_version, load_transcript, next_output, catalog,
                              prepare_record, record_details, timeline)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from console_pipeline import LiveProgress, say, summary
EXTENSIONS = {'.mp4', '.mov', '.mkv', '.avi', '.m4v', '.webm', '.mts', '.m2ts'}
SECTION = '## Cortes de silêncio'


def stamp(seconds):
    milliseconds = max(0, round(seconds * 1000))
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}.{ms:03}'


def report(folder, body, title=None, section=SECTION):
    path = folder / 'RELATORIO.md'
    text = path.read_text(encoding='utf-8-sig') if path.exists() else f'# Relatório da edição — {folder.name}\n'
    heading = title or 'Execução'
    entry = f'\n### {heading} — {datetime.now():%Y-%m-%d %H:%M:%S}\n\n{body}\n'
    match = re.search(r'^' + re.escape(section) + r'\s*$', text, re.MULTILINE)
    if match:
        following = re.search(r'^## ', text[match.end():], re.MULTILINE)
        position = match.end() + following.start() if following else len(text)
        text = text[:position].rstrip() + '\n' + entry + '\n' + text[position:]
    else:
        text = text.rstrip() + '\n\n' + section + '\n' + entry
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def media_info(path):
    executable = shutil.which('ffprobe')
    if not executable:
        raise RuntimeError('FFprobe nao encontrado no PATH. Instale FFmpeg.')
    result = subprocess.run([executable, '-v', 'error', '-show_format', '-show_streams',
                             '-of', 'json', str(path)], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=60)
    if result.returncode:
        raise RuntimeError('FFprobe: ' + result.stderr[-1000:])
    return json.loads(result.stdout)


def ffmpeg_run(args, label, duration, debug):
    executable = shutil.which('ffmpeg')
    if not executable:
        raise RuntimeError('FFmpeg nao encontrado no PATH.')
    command = [executable, '-hide_banner', '-nostdin', '-nostats', '-progress', 'pipe:1'] + args
    processed = 0.0
    messages = []
    lines = queue.Queue()
    with LiveProgress(label, inline=True) as progress:
        log = debug
        log.write(f'\n{datetime.now().isoformat()} {label}\n{command!r}\n')
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace', bufsize=1)

        def reader():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    line = lines.get(timeout=0.25)
                    if line is None:
                        break
                    messages.append(line)
                    log.write(line)
                    if line.startswith('out_time_us='):
                        try:
                            processed = max(0, int(line.partition('=')[2]) / 1_000_000)
                            progress.update(processed / duration if duration else None)
                        except ValueError:
                            pass
                except queue.Empty:
                    pass
            code = process.wait()
            thread.join()
            if code:
                raise RuntimeError(f'FFmpeg falhou ({code}). ' + ''.join(messages)[-1800:])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            thread.join(timeout=2)
            process.stdout.close()
    return ''.join(messages)


def parse_silences(text, duration):
    intervals = []
    start = None
    for match in re.finditer(r'silence_(start|end):\s*(-?[\d.]+)', text):
        kind, value = match.group(1), float(match.group(2))
        if kind == 'start':
            start = max(0.0, value)
        elif start is not None:
            intervals.append((start, min(duration, value)))
            start = None
    if start is not None:
        intervals.append((start, duration))
    return intervals


def make_plan(silences, duration, pause):
    cuts = []
    for start, end in silences:
        start, end = max(0, start), min(duration, end)
        if end - start <= pause + 0.000001:
            continue
        # Nas bordas preserva a pausa junto da fala; no meio, metade de cada lado.
        if start <= 0.0001:
            left, right = start, end - pause
        elif end >= duration - 0.0001:
            left, right = start + pause, end
        else:
            left, right = start + pause / 2, end - pause / 2
        cuts.append({'start': left, 'end': right, 'reason': 'silence',
                     'detected_start': start, 'detected_end': end})
    keeps = []
    cursor = edited = 0.0
    for cut in cuts + [{'start': duration, 'end': duration}]:
        if cut['start'] > cursor + 0.000001:
            length = cut['start'] - cursor
            keeps.append({'source_start': cursor, 'source_end': cut['start'],
                          'output_start': edited, 'output_end': edited + length})
            edited += length
        cursor = cut['end']
    return cuts, keeps, edited


def baseline_filters(config):
    audio = config.get('audio_baseline', {})
    image = config.get('image_baseline', {})
    audio_filter = ''
    image_filter = ''
    descriptions = []
    if audio.get('enabled', False):
        reduction = float(audio.get('noise_reduction_db', 3))
        if not 0.01 <= reduction <= 6:
            raise ValueError('Reducao de ruido basal deve ficar entre 0.01 e 6 dB.')
        audio_filter = f'afftdn=nr={reduction}:nf=-50:tn=1:gs=6,'
        descriptions.append(f'Áudio: redução espectral leve de ruído ({reduction:g} dB), com suavização de ganho; sem compressor ou mudança de pitch.')
    else:
        descriptions.append('Redução de ruído desativada.')
    descriptions.append('Eco/reverberação: remoção não aplicada; exige ajuste específico para evitar alterar timbre e finais de palavra.')
    if image.get('enabled', False):
        contrast = float(image.get('contrast', 1.03))
        saturation = float(image.get('saturation', 1.04))
        sharpness = float(image.get('sharpness', 0.2))
        vignette = float(image.get('vignette_angle', 0.261799))
        if not (0.9 <= contrast <= 1.1 and 0.9 <= saturation <= 1.1 and 0 <= sharpness <= 0.5 and 0 <= vignette <= 0.4):
            raise ValueError('Parametros de imagem fora da faixa leve prevista para o tratamento basal.')
        image_filter = (f',eq=contrast={contrast}:saturation={saturation},'
                        f'unsharp=5:5:{sharpness}:5:5:0,vignette=angle={vignette}')
        descriptions.append(f'Imagem: contraste {contrast:g}; saturação {saturation:g}; nitidez {sharpness:g} só na luminância; vinheta suave ({vignette:g} rad). Aplicado após o gerenciamento de cor.')
    else:
        descriptions.append('Tratamento basal de imagem desativado.')
    return audio_filter, image_filter, descriptions


def filter_graph(cuts, keeps, fade_ms, speed=1.0, color_filter='format=yuv420p', audio_filter='', image_filter=''):
    # Video sem split: evita manter varios trechos de video em memoria.
    conditions = '+'.join(f'gte(t,{k["source_start"]:.9f})*lt(t,{k["source_end"]:.9f})' for k in keeps)
    removed = '+'.join(f'{c["end"]-c["start"]:.9f}*gte(T,{c["end"]:.9f})' for c in cuts) or '0'
    graph = [f"[0:v:0]setpts=PTS-STARTPTS,select='{conditions}',setpts='(PTS-({removed})/TB)/{speed:.9f}',{color_filter}{image_filter}[vout]"]
    if len(keeps) > 1:
        graph.append('[1:a:0]asplit=' + str(len(keeps)) + ''.join(f'[a{i}]' for i in range(len(keeps))))
    for index, keep in enumerate(keeps):
        source = f'[a{index}]' if len(keeps) > 1 else '[1:a:0]'
        length = keep['source_end'] - keep['source_start']
        fade = min(fade_ms / 1000, length / 4)
        effects = f',afade=t=in:d={fade:.9f},afade=t=out:st={length-fade:.9f}:d={fade:.9f}' if fade > 0 else ''
        graph.append(f'{source}atrim=start={keep["source_start"]:.9f}:end={keep["source_end"]:.9f},asetpts=PTS-STARTPTS{effects}[ac{index}]')
    graph.append(''.join(f'[ac{i}]' for i in range(len(keeps))) + f'concat=n={len(keeps)}:v=0:a=1,{audio_filter}atempo={speed:.9f},apad[aout]')
    return ';\n'.join(graph)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(folder, debug=None, recovery=None, show_header=True):
    began = time.monotonic()
    if show_header:
        say('agente recuperador de pausas por Daniel Fileto' if recovery else 'agente de corte por Daniel Fileto')
        print('\n', flush=True)
    debug = debug if debug is not None else io.StringIO()
    config = (copy.deepcopy(recovery['base']['config']) if recovery
              else json.loads((ROOT / 'config-cortes.json').read_text(encoding='utf-8-sig')))
    audio_filter, image_filter, baseline = baseline_filters(config)
    threshold = float(config['silence_threshold_dbfs'])
    pause = float(config['pause_seconds'])
    target = float(config['normalize_peak_dbfs'])
    speed = float(config.get('playback_speed', 1))
    if not (-90 < threshold < target < 0 and 0 < pause < 10 and 0.5 <= speed <= 2):
        raise ValueError('Configuracao invalida: conferir limiar, pico e pausa.')
    if recovery:
        name = recovery['base']['source']['name']
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError('Nome do original invalido no historico.')
        original = folder / name
        videos = [original] if original.is_file() else []
    else:
        videos = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS and not is_version(p))
    if len(videos) != 1:
        raise ValueError(f'Coloque exatamente UM video original nesta pasta. Encontrados: {len(videos)}.')
    video = videos[0]
    print(f'Arquivo 1: {video.name}', flush=True)
    with LiveProgress('1/5 Localizando original e versão' if recovery else '1/5 Localizando vídeo e transcrição', inline=True) as progress:
        info = media_info(video)
        streams = info['streams']
        vstream = next((s for s in streams if s['codec_type'] == 'video'), None)
        if not vstream or not any(s['codec_type'] == 'audio' for s in streams):
            raise ValueError('O arquivo precisa conter video e audio.')
        colors = color_plan(vstream)
        color_text = color_description(colors)
        report(folder, f'- Status: INICIADO\n- Original: `{video.name}`\n- Tratamento de cor planejado:\n\n{color_text}')
        duration = float(vstream.get('duration') or info['format']['duration'])
        rate = vstream.get('avg_frame_rate', '0/0')
        if rate == '0/0':
            rate = vstream.get('r_frame_rate', '30/1')
        fps = float(Fraction(rate))
        if fps <= 0:
            raise ValueError('Nao foi possivel identificar FPS.')
        digest = hashlib.sha256()
        with video.open('rb') as stream:
            read_bytes = 0
            size = max(1, video.stat().st_size)
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                digest.update(chunk)
                read_bytes += len(chunk)
                progress.update(read_bytes / size)
        if recovery:
            expected = recovery['base']['source'].get('sha256')
            if not expected or expected != digest.hexdigest():
                raise ValueError('O original foi alterado ou nao corresponde a versao escolhida.')
            previous = folder / recovery['base']['output']
            expected_output = recovery['base'].get('output_sha256')
            if previous.is_file() and expected_output and file_hash(previous) != expected_output:
                raise ValueError('O MP4 da versao escolhida foi alterado fora da pipeline; seus tempos podem ter mudado.')
        transcript = load_transcript(folder, video, digest.hexdigest())
    output, version = next_output(folder, video)
    measured = ffmpeg_run(['-i', str(video), '-map', '0:a:0', '-vn', '-af', 'volumedetect', '-f', 'null', '-'],
                           '2/5 Analisando áudio', duration, debug)
    match = re.search(r'max_volume:\s*(-?[\d.]+) dB', measured)
    if not match:
        raise ValueError('Nao foi possivel medir o pico do audio.')
    peak = float(match.group(1))
    if peak <= -80:
        raise ValueError('Audio praticamente mudo: nao e seguro normalizar automaticamente.')
    gain = target - peak
    if recovery and recovery['base'].get('normalization'):
        gain = float(recovery['base']['normalization']['gain_db'])
    with tempfile.TemporaryDirectory(prefix='.silencio_tmp_', dir=folder) as temporary:
        temp = Path(temporary)
        audio = temp / 'normalizado.wav'
        filters = (f'aresample=48000:async=1:first_pts=0,volume={gain:.6f}dB,'
                   f'apad=whole_dur={duration:.9f},atrim=end={duration:.9f}')
        if not recovery:
            filters += f',silencedetect=noise={threshold}dB:d={pause:.9f}'
        detected = ffmpeg_run(['-i', str(video), '-map', '0:a:0', '-vn', '-af', filters,
                              '-c:a', 'pcm_f32le', '-y', str(audio)],
                             '3/5 Restaurando pausas selecionadas' if recovery else '3/5 Normalizando áudio e identificando pausas', duration, debug)
        restored = []
        if recovery:
            previous_record = prepare_record(recovery['base'], transcript)
            selected = set(recovery['selected'])
            available = {cut['id'] for cut in previous_record['removed']}
            if not selected or selected - available:
                raise ValueError('Selecione somente cortes ativos da versao escolhida.')
            all_cuts = previous_record['all_cuts']
            cuts = [copy.deepcopy(cut) for cut in previous_record['removed'] if cut['id'] not in selected]
            restored = sorted(set(previous_record['restored_pause_ids']) | selected)
            silences = [(c.get('detected_start', c['start']), c.get('detected_end', c['end'])) for c in all_cuts]
            keeps, cut_duration = timeline(cuts, duration)
        else:
            silences = parse_silences(detected, duration)
            if sum(end-start for start, end in silences) >= duration - 0.01:
                raise ValueError('Todo o audio ficou abaixo do limiar. Confira o limiar em config-cortes.json.')
            cuts, keeps, cut_duration = make_plan(silences, duration, pause)
            all_cuts = catalog(cuts, transcript)
            cuts = copy.deepcopy(all_cuts)
        edited_duration = cut_duration / speed
        if not keeps or edited_duration <= 0:
            raise ValueError('Plano de cortes vazio.')
        # Mapear diretamente para o tempo final, incluindo a aceleracao.
        final_map = [{**keep, 'output_start': keep['output_start']/speed,
                      'output_end': keep['output_end']/speed, 'playback_speed': speed} for keep in keeps]
        plan = {'schema_version': 3, 'stage': 'pause_recovery' if recovery else 'silence_cuts',
                'version': version, 'parent_output': recovery['base']['output'] if recovery else None,
                'restored_pause_ids': restored, 'all_cuts': all_cuts,
                'recovered_in_this_version': sorted(recovery['selected']) if recovery else [],
                'created_at': datetime.now().astimezone().isoformat(),
                'source': {'name': video.name, 'sha256': digest.hexdigest()}, 'config': config,
                'normalization': {'method': 'fixed_peak_gain', 'original_peak_dbfs': peak, 'gain_db': gain},
                'color_management': colors, 'playback_speed': speed, 'duration_before_speed': cut_duration,
                'baseline_treatment': baseline,
                'pause_seconds_before_speed': pause, 'pause_seconds_after_speed': pause/speed,
                'detected_pause_count': len(silences), 'cut_pause_count': len(cuts),
                'original_duration': duration, 'planned_output_duration': edited_duration,
                'detected_silences': [{'start': s, 'end': e} for s, e in silences],
                'removed': cuts, 'time_map': final_map,
                'notes': ['Tempos em segundos. Audio cortado em amostras; video quantizado aos frames.',
                          'Transcricao original nao foi remapeada. Usar time_map nas proximas etapas.',
                          'Limiar de amplitude nao distingue uma fala baixa de silencio.'], 'status': 'planned'}
        graph = temp / 'filtros.txt'
        graph.write_text(filter_graph(cuts, keeps, config['audio_fade_ms'], speed, colors['filter'], audio_filter, image_filter), encoding='utf-8')
        encoded = temp / 'resultado.mp4'
        ffmpeg_run(colors['ffmpeg_input_args'] + ['-i', str(video), '-i', str(audio), '-filter_complex_script', str(graph),
                    '-map', '[vout]', '-map', '[aout]', '-c:v', 'libx264', '-preset', config['video_preset'],
                    '-crf', str(config['video_crf']), '-threads', '4', '-filter_complex_threads', '1',
                    '-r', rate, '-fps_mode', 'cfr', '-c:a', 'aac', '-b:a', '192k',
                    '-t', f'{edited_duration:.9f}'] + OUTPUT_ARGS + ['-movflags', '+faststart', '-y', str(encoded)],
                   '4/5 Renderizando vídeo', edited_duration, debug)
        with LiveProgress('5/5 Conferindo arquivo exportado', inline=True):
            result = media_info(encoded)
            output_stream = next(s for s in result['streams'] if s['codec_type'] == 'video')
            validate_output(output_stream)
            actual = float(result['format']['duration'])
            if abs(actual - edited_duration) > max(0.15, 3 / fps):
                raise ValueError(f'Duracao exportada inesperada: {actual:.3f}s; prevista: {edited_duration:.3f}s.')
            if not all(any(s['codec_type'] == kind for s in result['streams']) for kind in ('audio', 'video')):
                raise ValueError('Exportacao sem audio ou video.')
            if output.exists():
                raise FileExistsError('O arquivo de saida ja existe; execute novamente para uma nova versao.')
            output_fingerprint = file_hash(encoded)
            encoded.replace(output)
            plan.update(status='completed', output=output.name, output_sha256=output_fingerprint, actual_output_duration=actual)
    # Metadados ficam no relatorio existente, recolhidos, sem arquivos auxiliares.
    action = 'Recuperação de pausas' if recovery else 'Corte de silêncios'
    recovery_text = (f'\n- Base: `{plan["parent_output"]}`\n- Pausas recuperadas nesta versão: '
                     + ', '.join(map(str, plan['recovered_in_this_version'])) + '.'
                     + '\n- Pausas recuperadas acumuladas: ' + ', '.join(map(str, restored)) + '.') if recovery else ''
    report(folder, f'- Status: CONCLUÍDO\n- Original: `{video.name}`\n- Resultado: [[{output.name}]]'
           f'\n- Versão: v{version:03}; operação: {action}.{recovery_text}'
           f'\n- Regras: pico {target:g} dBFS; limiar {threshold:g} dBFS; pausa máxima {pause*1000:g} ms.'
           f'\n- Ganho aplicado: {gain:+.1f} dB.\n' + '\n'.join('- ' + item for item in baseline) + '\n'
           f'\n- Pausas detectadas: {len(silences)}; pausas encurtadas: {len(cuts)}; tempo removido por cortes: {stamp(duration-cut_duration)}.'
           f'\n- Velocidade: {speed:g}× com pitch preservado; pausa final aproximada: {pause/speed*1000:.1f} ms.'
           f'\n- Cor e cuidados aplicados:\n\n{color_text}\n'
           f'\n- Duração original/final: {stamp(duration)} / {stamp(actual)}.'
           f'\n- Tempo de processamento: {stamp(time.monotonic()-began)}.'
           '\n- Somente silêncios nesta versão; reinícios/gaguejadas ainda não implementados.'
           '\n- Aviso de calibração: fala abaixo do limiar também pode ser classificada como silêncio.'
           '\n\n' + record_details(plan, transcript),
           title=f'v{version:03} — {action}', section='## Histórico de versões')
    summary('CONCLUÍDO', [
        f'Pausas encurtadas: {len(cuts)} de {len(silences)}',
        *([f'Pausas recuperadas: {", ".join(map(str, plan["recovered_in_this_version"]))}'] if recovery else []),
        f'Velocidade: {speed:g}x',
        f'Cor inicial: {colors["source_label"]}',
        'Cor final: Rec.709 SDR',
        f'Tempo total: {stamp(time.monotonic()-began)}',
        f'Resultado: {output}', 'Relatório atualizado.',
    ])
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=ROOT)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if not folder.is_dir():
        parser.error('Pasta inexistente.')
    debug = io.StringIO()
    try:
        run(folder, debug)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        detail = f'{type(error).__name__}: {error}'
        diagnostics = traceback.format_exc() + '\n' + debug.getvalue()[-10000:]
        report(folder, f'- Status: {"INTERROMPIDO" if isinstance(error, KeyboardInterrupt) else "ERRO"}\n- Detalhe: {detail}'
               f'\n\n<details>\n<summary>Diagnóstico técnico</summary>\n\n```text\n{diagnostics}\n```\n\n</details>')
        summary('PROCESSO INTERROMPIDO' if isinstance(error, KeyboardInterrupt) else 'ERRO NO PROCESSAMENTO',
                ['Não foi possível concluir o processamento.', 'Relatório atualizado.'], 'red')
        return 1


if __name__ == '__main__':
    sys.exit(main())
