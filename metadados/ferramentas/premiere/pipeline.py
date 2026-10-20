"""Entrada única do BAT: processamento local -> Gemini -> projeto Premiere."""
from __future__ import annotations
import argparse
import contextlib
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import traceback

from exportar_premiere import (CENTRAL, DEFAULT_CLIENTS, CONFIG, discover, probe,
                               analyze as detect_pauses, file_hash, write_treated_audio,
                               build_xml, validate_xml, report, timeline_grid)
from legendar_premiere import (settings, get_transcript, map_words, make_cues,
                               srt_text, srt_clock, atomic_json)
from analise_editorial import analyze as editorial_analysis, apply_repetitions, title_timeline, map_anchor
from montar_nativo import build
from progresso_etapas import StageProgress
from console_pipeline import LiveProgress, say, summary
from geometria_video import CANVAS, fit_plan
from fila_edicoes import choose_jobs, SelectionCancelled
from controle_execucao import control, pending_files
from converter_hdr import prepare as prepare_hdr
from exportar_premiere import ffmpeg_run
from ambiente_online import logs_folder, model_path as configured_model, work_edition

VERSION = 1


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def next_version(edition):
    versions = [0]
    for folder in (edition, edition/'premiere', edition/'metadados'):
        for path in folder.glob('*'):
            if not path.is_file() or (folder == edition and path.suffix.casefold() != '.prproj'):
                continue
            match = re.search(r'-v(\d+)', path.stem, re.IGNORECASE)
            if match:
                versions.append(int(match[1]))
    return max(versions)+1


def preflight():
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            raise RuntimeError(f'{tool} não encontrado no PATH.')
    from importlib.metadata import version
    if version('av') != '17.1.0':
        raise RuntimeError('Este ambiente requer av 17.1.0 para o faster-whisper instalado.')
    from gemini_cliente import api_key
    api_key()  # Apenas existência local; não envia requisição.
    config = json.loads((CENTRAL/'config'/'pipeline.json').read_text(encoding='utf-8-sig'))
    if config.get('editor') == 'capcut':
        from backend_capcut import preflight as capcut_preflight
        capcut_preflight(config)
    hdr = json.loads((CENTRAL/config['cor']['preset_hdr_sdr']).read_text(encoding='utf-8'))
    if not hdr.get('filter') or not hdr.get('preset'):
        raise RuntimeError('Preset HDR→SDR ausente ou inválido.')
    if not configured_model(CENTRAL, config).is_file():
        raise RuntimeError('Modelo nativo central ausente.')
    from gerar_prproj import read_project
    from titulos_nativos import inspect_templates
    native = read_project(configured_model(CENTRAL, config))
    inspect_templates(native)
    if len(native.findall('AudioClipTrack')) != 2:
        raise RuntimeError('Modelo deve conter exatamente as duas faixas de áudio estudadas.')
    if not (CENTRAL/'prompts'/'BRIEFING_EDITORIAL.md').is_file():
        raise RuntimeError('Briefing editorial central ausente.')
    return config


def process(edition, videos, config, force=False):
    started = time.monotonic()
    if os.environ.get('PREEDITOR_WORKSPACE_ROOT'):
        edition.mkdir(parents=True, exist_ok=True)
    metadata, output_dir = edition/'metadados', edition/('CapCut' if config.get('editor') == 'capcut' else 'premiere')
    metadata.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    lock = metadata/'processamento.lock'
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError:
        raise RuntimeError('Esta edição já tem processamento.lock. Verifique se outra execução está aberta; remova o lock apenas se ela já terminou.') from None
    with handle:
        handle.write(f'PID {os.getpid()} — {datetime.now().isoformat()}')
    try:
        return _process(edition, videos, config, started, force)
    finally:
        lock.unlink(missing_ok=True)


def _process(edition, videos, config, started, force=False):
    metadata, output_dir = edition/'metadados', edition/('CapCut' if config.get('editor') == 'capcut' else 'premiere')
    model_path = configured_model(CENTRAL, config)
    hdr_preset = json.loads((CENTRAL/config['cor']['preset_hdr_sdr']).read_text(encoding='utf-8'))
    options = settings()
    with LiveProgress('Conferindo alterações dos arquivos', inline=True):
        files = [{'path': str(v.resolve()), 'sha256': file_hash(v)} for v in videos]
    signature = {'version': VERSION, 'files': files,
                 'config': config, 'hdr_preset': hdr_preset, 'silence': CONFIG, 'transcription': options,
                 'implementation': fingerprint({p.name: file_hash(p) for p in Path(__file__).parent.glob('*.py') if not p.name.startswith('test_')}),
                 'model': file_hash(model_path),
                 'briefing': file_hash(CENTRAL/'prompts'/'BRIEFING_EDITORIAL.md')}
    if config.get('editor') == 'capcut':
        from backend_capcut import NATIVE
        signature['capcut'] = fingerprint({str(p.relative_to(NATIVE)): file_hash(p)
            for p in NATIVE.rglob('*') if p.is_file() and p.suffix in {'.py', '.json'}
            and 'Backups' not in p.parts})
    signature_hash = fingerprint(signature)
    state_path = metadata/('estado-capcut.json' if config.get('editor') == 'capcut' else 'estado-pipeline.json')
    if state_path.exists() and not force:
        previous = json.loads(state_path.read_text(encoding='utf-8'))
        # Não sobrescreve o PRPROJ que o editor já tenha modificado.
        if previous.get('signature') == signature_hash and all((edition/p).is_file() for p in previous['outputs']):
            say('Já preparado; projeto anterior preservado.', 'yellow')
            return {'status': 'reutilizado', 'seconds': time.monotonic()-started,
                    'project': previous['project']}
    timings = {}
    stage_started = time.monotonic()

    def stage_done(progress, key):
        nonlocal stage_started
        timings[key] = round(time.monotonic()-stage_started, 3)
        progress.done()
        stage_started = time.monotonic()

    with StageProgress(total=7) as progress:
        progress.stage('Conferindo os arquivos')
        with contextlib.redirect_stdout(io.StringIO()):
            sources = [probe(v) for v in videos]
        stage_done(progress, 'conferencia')
        for i, source in enumerate(sources):
            progress.stage(f'{"Conferindo cor original" if config.get("editor") == "capcut" else "Preparando cor Rec.709"} — arquivo {i+1}/{len(sources)}')
            sources[i] = (source if config.get('editor') == 'capcut' else
                          prepare_hdr(source, metadata, hdr_preset, ffmpeg_run, probe, file_hash))
            if sources[i].get('hdr_conversion'):
                reused_sdr = sources[i].get('sdr_reused', False)
                progress.stage(f'{"SDR existente reaproveitado" if reused_sdr else "Conversão SDR concluída"} — arquivo {i+1}/{len(sources)}')
                record = sources[i]['hdr_conversion']
                preset_note = ('Preset original não comprovado: SDR existente validado, sem alterar a cor.'
                               if record.get('preset_verified') is False else
                               f'Preset registrado: {record["preset"]}.')
                report(metadata, f'- `{source["name"]}`: HLG/BT.2020 → Rec.709 SDR.\n'
                       f'- {"SDR existente reaproveitado; nenhuma conversão executada." if reused_sdr else "Nova conversão executada."}\n'
                       f'- {preset_note}\n'
                       f'- Cópia: `{sources[i]["name"]}`; 10 bits, resolução preservada, áudio sem recodificação.\n'
                       '- Original intacto; cópia SDR verificada/reaproveitável. Sem override Rec.709.',
                       title='Preparação HDR → SDR', section='## Cor')
        if len({s['channels'] for s in sources}) != 1:
            raise ValueError('Os arquivos da edição devem ter o mesmo número de canais de áudio.')
        # FPS de fontes pode variar: todos os cortes usam segundos e uma grade comum.
        sources = [timeline_grid(s, sources[0]['fps']) for s in sources]
        for source in sources:
            fit_plan(source)  # Valida orientação e calcula encaixe; não converte mídia.
        stage_done(progress, 'cor')

        transcripts, transcript_paths = [], []
        for i, source in enumerate(sources, 1):
            progress.stage(f'Transcrevendo localmente — arquivo {i}/{len(sources)}: {source["name"]}')
            with contextlib.redirect_stdout(io.StringIO()):
                transcript, path = get_transcript(source, metadata, options)
            transcripts.append(transcript)
            transcript_paths.append(path)
        stage_done(progress, 'transcricao')

        base_plans = []
        for i, source in enumerate(sources, 1):
            progress.stage(f'Identificando pausas e preparando áudio — arquivo {i}/{len(sources)}')
            cache_signature = fingerprint({'source': source, 'silence': CONFIG, 'audio': config['audio']})
            cache_path = metadata/f'audio-cortes-{cache_signature[:16]}.json'
            cached = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.is_file() else None
            audio_signature = fingerprint({'source_sha256': source['sha256'], 'audio': config['audio'], 'duration': source['duration']})
            wav = output_dir/'audio'/f'{i:03}-{source["sha256"][:12]}-{audio_signature[:8]}.wav'
            audio_cache_path = metadata/f'audio-tratado-{audio_signature[:16]}.json'
            if cached and wav.is_file() and file_hash(wav) == cached['treated_audio']['sha256']:
                plan = cached
            else:
                treated = json.loads(audio_cache_path.read_text(encoding='utf-8')) if audio_cache_path.is_file() else None
                if treated and (not Path(treated['path']).is_file() or file_hash(Path(treated['path'])) != treated['sha256']):
                    raise ValueError('Áudio tratado do cache ausente/alterado; preserve o projeto e revise o arquivo antes de refazer.')
                if not treated and wav.exists():
                    raise ValueError(f'Áudio intermediário sem cache válido: {wav.name}. Preserve-o e mova-o antes de refazer; o programa não sobrescreve arquivos.')
                with contextlib.redirect_stdout(io.StringIO()):
                    debug = io.StringIO()
                    plan = detect_pauses(source, debug)
                    paths = [audio_cache_path, cache_path, audio_cache_path.with_suffix('.json.tmp'), cache_path.with_suffix('.json.tmp')]
                    if not treated:
                        paths.append(wav)
                    with pending_files(paths):
                        plan['treated_audio'] = treated or write_treated_audio(source, wav, debug, config['audio'])
                        # Campos de edição não obrigam reprocessamento do som.
                        plan['treated_audio'].update(fps=source['fps'], frames=source['frames'], duration=source['duration'])
                        atomic_json(audio_cache_path, plan['treated_audio'])
                        atomic_json(cache_path, plan)
            base_plans.append(plan)
        stage_done(progress, 'pausas_audio')

        progress.stage('Analisando o texto e selecionando títulos')
        analysis, words, cache, reused = editorial_analysis(metadata, transcripts)
        editorial_record = json.loads(cache.read_text(encoding='utf-8'))
        editorial_model = editorial_record.get('model', 'não informado')
        editorial_adjustments = editorial_record.get('local_adjustments', [])
        plans = apply_repetitions(base_plans, words, analysis)
        titles = title_timeline(analysis, words, plans)
        stage_done(progress, 'gemini')

        progress.stage('Sincronizando legendas e preparando as camadas')
        mapped, warnings, duration = [], [], 0.0
        for plan, transcript in zip(plans, transcripts):
            items, notes, duration = map_words(plan, transcript, duration)
            mapped.extend(items)
            warnings.extend(notes)
        cues = make_cues(mapped, duration)
        if not cues or any(len(c['text'].splitlines()) > 2 for c in cues):
            raise ValueError('Legendas vazias ou com mais de duas linhas; confira a transcrição.')
        if config.get('editor') == 'capcut':
            from backend_capcut import finish
            return finish(edition, plans, cues, titles, progress, stage_done, config,
                          {'signature': signature_hash, 'input': signature, 'analysis': analysis,
                           'transcripts': [str(p.relative_to(edition)) for p in transcript_paths],
                           'editorial_model': editorial_model, 'started': started, 'files': files})
        version = next_version(edition)
        project = output_dir/f'{edition.parent.name} - {edition.name}-v{version:03}.prproj'
        subtitle = output_dir/f'legendas-{edition.parent.name} - {edition.name}-v{version:03}.srt'
        xml_path = metadata/f'timeline-v{version:03}.xml'
        xml = build_xml(edition.name, plans, sequence_size=CANVAS)
        validate_xml(xml, check_files=True)
        stage_done(progress, 'legendas')

        progress.stage('Montando e conferindo o projeto do Premiere')
        data, checks = build(model_path, plans, cues, titles, project, config['titulos']['animar_fade'], config.get('cor'))
        if any(file_hash(Path(s['path'])) != s['sha256'] for s in sources):
            raise ValueError('Um original mudou durante o processamento.')
        if any(file_hash(Path(f['path'])) != f['sha256'] for f in files):
            raise ValueError('Um bruto mudou durante o processamento.')
        if file_hash(model_path) != signature['model'] or file_hash(CENTRAL/'prompts'/'BRIEFING_EDITORIAL.md') != signature['briefing']:
            raise ValueError('Configuração/modelo/briefing mudou durante a execução.')
        if json.loads((CENTRAL/config['cor']['preset_hdr_sdr']).read_text(encoding='utf-8')) != hdr_preset:
            raise ValueError('Preset HDR→SDR mudou durante a execução.')
        control(phase='publishing')  # ESC aguarda esta curta gravação, não deixa PRPROJ pela metade.
        for path, body in [(project, data), (subtitle, srt_text(cues).encode('utf-8-sig')), (xml_path, xml)]:
            with path.open('xb') as stream:
                stream.write(body)
        stage_done(progress, 'montagem')
        progress.label = 'Projeto de pré-edição finalizado'
    elapsed = round(time.monotonic()-started, 3)
    visuals = [{**v, 'start': map_anchor(next(w for w in words if w['id'] == v['ancora']), plans)}
               for v in analysis['visuais']]
    record = {'version': version, 'signature': signature_hash, 'input': signature,
              'reprocess_requested': force,
              'created_at': datetime.now().astimezone().isoformat(), 'sources': plans,
              'transcripts': [str(p.relative_to(edition)) for p in transcript_paths],
              'titles': titles, 'visuals': visuals, 'analysis': analysis,
              'editorial_model': editorial_model,
              'editorial_adjustments': editorial_adjustments,
              'checks': checks, 'timings': timings, 'elapsed_seconds': elapsed,
              'status': 'gerado; conferir abertura, sincronismo e aparência no Premiere 2026',
              'project': str(project.relative_to(edition)), 'sha256': file_hash(project)}
    atomic_json(metadata/f'plano-nativo-v{version:03}.json', record)
    atomic_json(state_path, {'signature': signature_hash, 'project': str(project.relative_to(edition)),
                            'outputs': [str(p.relative_to(edition)) for p in (project, subtitle, xml_path)]})
    reading = '\n'.join(f'- {srt_clock(c["start_ms"])[3:8]} — {c["text"].replace(chr(10), " ")}' for c in cues)
    visual_text = '\n'.join(f'- {v["start"]:.2f}s: ({v["descricao"]})' for v in visuals if v['start'] is not None) or '- Nenhuma.'
    geometry_text = '\n'.join(f'- `{s["name"]}`: armazenado {s["width"]}×{s["height"]}, rotação {s.get("rotation_degrees", 0):g}°; '
                              f'exibição {g["display_size"][0]}×{g["display_size"][1]}; escala {g["scale_percent"]:.3f}% em 1080×1920, '
                              f'Motion editável, fonte original preservada. Cor: {s["color"]}.'
                              for s, g in zip(sources, checks['geometry']))
    color_text = '\n'.join(f'- `{c["source"]}`: {c["status"]}.' for c in checks['color'])
    cadence_text = '\n'.join(f'- `{s["name"]}`: {s["cadence"]["mode"]}; média original {s["media_fps"]} fps; '
                             f'grade de edição {s["fps"]} fps; duração real {s["duration"]:.6f}s. '
                             'Sem mudança de velocidade; conversão HDR→SDR quando registrada na seção Cor.' for s in sources)
    report(metadata,
           f'- Resultado: `../premiere/{project.name}`; {duration:.2f}s, velocidade 1×.\n'
           '- V1 original cortado; V2 ajuste/Lumetri; V3 títulos de posição fixa. Legendas nativas em faixa própria.\n'
           '- A1 original desativado; A2 tratado ativo. Canais preservados, Level 0 dB; pico-alvo 0 dBFS com aumento total limitado a +6 dB por trecho (WAV + clipe).\n'
           '- WAV completo: redução de ruído leve, sem mudança de pitch, compressor ou remoção automática de eco. Original intocado.\n'
           f'- {sum(len(s["removed_frames"]) for s in base_plans)} pausas encurtadas a pelo menos 200 ms; '
           f'{len(analysis["repeticoes"])} grupos de retomadas aplicados; {len(analysis["revisar_repeticoes"])} casos ambíguos somente para revisão.\n'
           f'- {len(cues)} legendas, até cinco palavras/duas linhas, separadas também por pontuação; {checks["titles"]} títulos com fade simples editável. MOGRTs personalizados/CTA serão integrados quando o catálogo for fornecido.\n'
           f'- Gemini: {editorial_model}; cache {"reutilizado" if reused else "novo"}. Apenas transcrição; nenhuma imagem, vídeo ou áudio enviado.\n'
           f'- Ajustes locais de títulos: {len(editorial_adjustments)} negação(ões) falada(s) preservada(s); detalhes no plano/cache editorial.\n'
           '- Arquivos originais preservados; Lumetri removível. Override altera a interpretação no projeto, não faz tone mapping HDR→SDR.\n'
           f'{color_text}\n'
           f'{geometry_text}\n'
           f'{cadence_text}\n'
           '- Mídia VFR: conferir sincronismo no Premiere; leitura dos timestamps originais pelo importador. Fração final menor que um frame fica fora da grade, sem cortar o arquivo original.\n'
           f'- Tempo total: {elapsed:.2f}s. Tempos por etapa: {timings}.\n'
           '- SRT ao lado do projeto; XML técnico de contingência nesta pasta não contém enquadramento, efeitos ou gráficos nativos do PRPROJ.\n'
           '- Conferência estrutural aprovada; revisão no Premiere permanece obrigatória. Cortes e áudio tratado têm handles completos para recuperar pausas.\n\n'
           f'#### Sugestões visuais\n\n{visual_text}\n\n'
           f'#### Leitura pós-corte\n\n{reading}\n\n'
           f'#### Revisão de repetições\n\n{json.dumps(analysis["revisar_repeticoes"], ensure_ascii=False)}\n\n'
           f'#### Avisos de alinhamento\n\n' + ('\n'.join('- '+w for w in warnings) or '- Nenhum.'),
           title=f'Pré-edição v{version:03}', section='## Pré-edição integrada')
    summary('Pré-edição pronta para revisar', [f'Projeto: {project}',
            f'Tempo total: {elapsed:.1f}s | Duração final: {duration:.1f}s',
            f'Legendas: {len(cues)} | Títulos: {checks["titles"]}',
            f'Cor SDR: {sum(bool(s.get("hdr_conversion")) and not s.get("sdr_reused", False) for s in sources)} conversão(ões); '
            f'{sum(bool(s.get("sdr_reused")) for s in sources)} cópia(s) reaproveitada(s).', 'Relatório atualizado.'])
    return {'status': 'gerado', 'project': str(project.relative_to(edition)), 'seconds': elapsed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clientes', type=Path, default=DEFAULT_CLIENTS)
    parser.add_argument('--verificar', action='store_true')
    args = parser.parse_args()
    began = time.monotonic()
    central_logs = logs_folder(CENTRAL)
    central_logs.mkdir(parents=True, exist_ok=True)
    results = []
    say('agente de pré-edição por Daniel Fileto')
    try:
        jobs = list(discover(args.clientes.resolve()))
        jobs = [(work_edition(edition, args.clientes), videos) for edition, videos in jobs]
        if args.verificar:
            preflight()
            summary('Verificação local concluída', [f'Edições encontradas: {len(jobs)}',
                    'Dependências, chave local e modelo de projeto presentes. Nenhuma requisição enviada.',
                    'Modelo de transcrição: conferir pelo instalador; esta verificação não o carrega.'])
            return 0
        editor = json.loads((CENTRAL/'config'/'pipeline.json').read_text(encoding='utf-8-sig')).get('editor', 'premiere')
        queue = choose_jobs(jobs, editor=editor)
        if not queue:
            say('Não há edições novas precisando de pré-edição.' if jobs else 'Nenhuma edição com vídeos encontrada.', 'yellow')
            report(central_logs, '- Nenhum trabalho iniciado; nenhum projeto existente foi selecionado para refazer.',
                   title='Varredura sem fila', section='## Execuções')
            return 0
        config = preflight()
        if config.get('editor') == 'capcut':
            from backend_capcut import runtime_ready
            runtime_ready(config)
        control(phase='processing')
        say('ESC para interromper a execução.', 'yellow')
        for i, (edition, videos, force) in enumerate(queue, 1):
            control(phase='processing', edition=str(edition.resolve()), pending=[])
            say(f'\nEdição {i}/{len(queue)} — {edition.parent.name} / {edition.name}')
            for j, video in enumerate(videos, 1):
                say(f'Arquivo {j}: {video.name}', 'muted')
            try:
                result = process(edition, videos, config, force=force)
            except Exception as error:
                result = {'status': 'erro', 'error': str(error)}
                say(f'ERRO: {error}', 'red')
                metadata = edition/'metadados'
                metadata.mkdir(exist_ok=True)
                report(metadata, f'- ERRO: {error}\n- Originais e versões anteriores preservados.',
                       title='Execução interrompida', section='## Pré-edição integrada')
                with (metadata/'pipeline-debug.log').open('a', encoding='utf-8') as stream:
                    stream.write(f'\n{datetime.now().isoformat()}\n{traceback.format_exc()}')
            results.append({'edition': str(edition), **result})
            control(phase='processing', edition=None, pending=[])
        report(central_logs, '\n'.join(f'- {r["edition"]}: {r["status"]}; {r.get("project", r.get("error", ""))}' for r in results)
               + f'\n- Tempo do lote: {time.monotonic()-began:.2f}s.', title='Lote', section='## Execuções')
        errors = sum(r['status'] == 'erro' for r in results)
        generated = sum(r['status'] == 'gerado' for r in results)
        reused = sum(r['status'] == 'reutilizado' for r in results)
        summary('Lote encerrado', [f'Edições: {len(results)} | Prontas: {generated} | Já prontas: {reused} | Falharam: {errors}',
                                  f'Tempo total: {time.monotonic()-began:.1f}s',
                                  *[f'FALHOU: {Path(r["edition"]).parent.name} / {Path(r["edition"]).name} — {r["error"]}'
                                    for r in results if r['status'] == 'erro'],
                                  'Nenhuma edição encontrada.' if not results else 'Conferir os projetos no editor.'])
        return int(errors > 0)
    except SelectionCancelled:
        say('Seleção cancelada; nenhum trabalho iniciado.', 'yellow')
        return 130
    except Exception as error:
        say(f'ERRO: {error}', 'red')
        report(central_logs, f'- ERRO: {error}', title='Preparação', section='## Execuções')
        return 1


if __name__ == '__main__':
    sys.exit(main())
