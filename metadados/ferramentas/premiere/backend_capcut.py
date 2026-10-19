"""Saída CapCut nativa do motor comum; sem render/conversão HDR do Premiere."""
import copy
import io
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import time

from exportar_premiere import CENTRAL, file_hash, ffmpeg_run, report
from legendar_premiere import atomic_json, srt_text
from controle_execucao import control, pending_files
from console_pipeline import summary

NATIVE = CENTRAL/'ferramentas/capcut'
sys.path.insert(0, str(NATIVE))


def preflight(config):
    from montar_capcut import load
    reference = NATIVE/'Metadados/Modelo/0806-original'
    doc = load(reference/'draft_content.json')
    if doc.get('version') != 360000 or doc['last_modified_platform']['app_version'] != '9.5.0':
        raise ValueError('Modelo CapCut incompatível com o perfil estudado 9.5.0.')


def store_path(config):
    return Path(config.get('capcut', {}).get('projects', '') or
                str(Path(os.environ['LOCALAPPDATA'])/'CapCut/User Data/Projects/com.lveditor.draft'))


def runtime_ready(config):
    running = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        '@(Get-Process -Name CapCut -ErrorAction SilentlyContinue).Count'],
        capture_output=True, text=True, check=True)
    if running.stdout.strip() != '0':
        raise RuntimeError('Feche o CapCut antes de iniciar a pré-edição; nenhum projeto será fechado automaticamente.')
    if not (store_path(config)/'root_meta_info.json').is_file():
        raise RuntimeError('Abra o CapCut desktop uma vez e feche-o para inicializar seus projetos; depois rode novamente.')


def finish(edition, plans, cues, titles, progress, stage_done, config, record):
    from montar_capcut import build, validate, original_media_sources
    from cadastrar_piloto import register
    metadata, base = edition/'metadados', edition/'CapCut'
    store = store_path(config)
    if not (store/'root_meta_info.json').is_file():
        raise RuntimeError('Abra o CapCut desktop uma vez e feche-o para inicializar a pasta de projetos. Depois rode novamente.')
    versions = [int(m.group(1)) for p in base.iterdir()
                if (m := re.search(r'-v(\d+)$', p.name))]
    version = max(versions, default=0)+1
    name = f'{edition.parent.name} - {edition.name}-v{version:03}'
    while (store/name).exists():
        version += 1
        name = f'{edition.parent.name} - {edition.name}-v{version:03}'
    folder = base/name
    sources = original_media_sources(plans)
    audio = base/'audio'
    audio.mkdir(exist_ok=True)
    for i, source in enumerate(sources, 1):
        original = audio/f'{i:03}-{source["sha256"][:16]}-original.wav'
        if not original.exists():
            with pending_files([original]):
                ffmpeg_run(['-n', '-i', source['path'], '-map', '0:a:0', '-vn',
                    '-c:a', 'pcm_s24le', '-ar', '48000', str(original)],
                    'Separando áudio original', source['duration'], io.StringIO())
        source['original_audio'] = {'path': str(original.resolve()), 'name': original.name}
    reference = NATIVE/'Metadados/Modelo/0806-original'
    doc, meta, index = build(reference, sources, cues, titles, name, folder)
    checks = validate(doc)
    stage_done(progress, 'legendas')
    progress.stage('Montando e cadastrando o projeto CapCut')
    if any(file_hash(Path(f['path'])) != f['sha256'] for f in record['files']):
        raise ValueError('Um bruto mudou durante o processamento.')
    if any(file_hash(Path(s['treated_audio']['path'])) != s['treated_audio']['sha256'] for s in sources):
        raise ValueError('Um áudio tratado mudou durante o processamento.')
    control(phase='publishing')
    timeline = folder/'Timelines'/doc['id']
    timeline.mkdir(parents=True, exist_ok=False)
    for path, data in [(folder/'draft_content.json', doc), (folder/'template-2.tmp', doc),
                       (timeline/'draft_content.json', doc), (timeline/'template-2.tmp', doc),
                       (folder/'draft_meta_info.json', meta), (folder/'Timelines/project.json', index)]:
        atomic_json(path, data)
    subtitle = folder/'legendas.srt'
    subtitle.write_text(srt_text(cues), encoding='utf-8-sig')
    registration = register(folder, store, name)
    record = copy.deepcopy(record)
    elapsed = time.monotonic()-record.pop('started')
    record.update(version=version, sources=sources, titles=titles, checks=checks,
                  project=str((folder/'draft_content.json').relative_to(edition)),
                  registered_project=registration['project'], elapsed_seconds=elapsed,
                  status='gerado; conferir aparência, sincronismo e salvamento no CapCut')
    atomic_json(metadata/f'plano-capcut-v{version:03}.json', record)
    atomic_json(metadata/'estado-capcut.json', {'signature': record['signature'],
        'project': record['project'], 'outputs': [record['project'], str(subtitle.relative_to(edition))]})
    report(metadata, f'- Projeto: `{name}`; cadastrado no CapCut.\n'
        f'- {checks["clips"]} clipes, {len(cues)} legendas (até cinco palavras/duas linhas; separadas por pontuação), {len(titles)} títulos simples.\n'
        '- Vídeo original/HDR intacto; áudio original mudo, áudio tratado ativo; textos separados/editáveis.\n'
        '- Pico-alvo 0 dBFS; aumento total limitado a +6 dB por trecho, contando WAV + ganho editável do clipe.\n'
        '- Sem conversão HDR, Lumetri, MOGRT, efeito 4K ou animações novas. Perfil estudado: CapCut 9.5.0.\n'
        f'- Relatório editorial e sugestões no plano-capcut-v{version:03}.json. Tempo: {elapsed:.1f}s.\n'
        '- Conferir reprodução, aparência e salvar/reabrir no aplicativo do editor.',
        title=f'CapCut v{version:03}', section='## Pré-edição CapCut')
    stage_done(progress, 'montagem')
    progress.label = 'Projeto CapCut finalizado'
    summary('Pré-edição CapCut pronta para revisar', [f'Projeto: {name}',
        f'Legendas: {len(cues)} | Títulos: {len(titles)} | Tempo total: {elapsed:.1f}s',
        'Abra o CapCut para conferir. Relatório atualizado.'])
    return {'status': 'gerado', 'project': record['project'], 'seconds': elapsed}
