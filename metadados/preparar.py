"""Preparação local do pacote privado; não imprime nem testa a chave na rede."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

class LibraryVersionError(RuntimeError):
    pass


def atomic_json(path, data):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


@contextmanager
def activity(label):
    stop = threading.Event()
    began = time.monotonic()
    def notify():
        while not stop.wait(15):
            print(f'{label}; aguardando/conferindo dados | {int(time.monotonic()-began)}s decorridos', flush=True)
    worker = threading.Thread(target=notify, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=1)

CENTRAL = Path(__file__).resolve().parent
sys.path.insert(0, str(CENTRAL/'ferramentas/premiere'))


def load(name):
    return json.loads((CENTRAL/name).read_text(encoding='utf-8'))


def integrity():
    manifest = load('manifesto.json')
    for name, expected in manifest['sha256'].items():
        path = (CENTRAL/name).resolve()
        if not path.is_relative_to(CENTRAL.resolve()) or not isinstance(expected, str) or len(expected) != 64:
            raise RuntimeError('Manifesto de integridade invalido.')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise RuntimeError(f'Arquivo incompleto/alterado: {name}. Extraia novamente o ZIP original.')
    print(f'Arquivos conferidos: {len(manifest["sha256"])}. Python privado {sys.version.split()[0]}.', flush=True)


def libraries():
    for name, version in load('manifesto.json')['dependencies'].items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise LibraryVersionError(f'Biblioteca ausente: {name}. Extraia novamente o pacote.') from None
        if actual != version:
            raise LibraryVersionError(f'Versao divergente: {name}. Extraia novamente o pacote; nao instalar VC++ para corrigir versoes.')
    import av
    import ctranslate2
    import onnxruntime
    import tokenizers
    from faster_whisper import WhisperModel
    print('Bibliotecas e DLLs carregadas; versoes conferidas.', flush=True)


def model():
    from faster_whisper.utils import download_model
    manifest = load('manifesto.json')
    cache = CENTRAL/'ferramentas/transcricao/.modelos'
    try:
        download_model('medium', cache_dir=str(cache), revision=manifest['whisper_revision'], local_files_only=True)
        print('Modelo encontrado no cache local; sem novo download.', flush=True)
    except Exception:
        print('Primeiro uso: download do modelo medium (~1,5 GB). Aguarde a barra de download.', flush=True)
        with activity('Preparando download do modelo'):
            download_model('medium', cache_dir=str(cache), revision=manifest['whisper_revision'])
    ref = cache/'models--Systran--faster-whisper-medium/refs/main'
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(manifest['whisper_revision'], encoding='utf-8')
    from faster_whisper import WhisperModel
    print('Conferindo carregamento local do modelo...', flush=True)
    try:
        with activity('Carregando modelo local'):
            WhisperModel('medium', device='cpu', compute_type='int8', local_files_only=True,
                         download_root=str(cache))
    except BaseException:
        atomic_json(CENTRAL/'modelo-pronto.json', {'revision': manifest['whisper_revision'], 'files': {}, 'status': 'falha ao carregar'})
        raise
    snapshot = cache/'models--Systran--faster-whisper-medium/snapshots'/manifest['whisper_revision']
    files = {name: (snapshot/name).stat().st_size for name in
             ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')}
    atomic_json(CENTRAL/'modelo-pronto.json', {'revision': manifest['whisper_revision'], 'files': files})


def model_ready():
    try:
        marker = load('modelo-pronto.json')
        manifest = load('manifesto.json')
        expected = {'model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt'}
        if marker['revision'] != manifest['whisper_revision'] or set(marker['files']) != expected:
            return False
        snapshot = CENTRAL/'ferramentas/transcricao/.modelos/models--Systran--faster-whisper-medium/snapshots'/marker['revision']
        ref = snapshot.parent.parent/'refs/main'
        if ref.read_text(encoding='utf-8').strip() != marker['revision']:
            return False
        return all(size > 0 and (snapshot/name).is_file() and (snapshot/name).stat().st_size == size
                   for name, size in marker['files'].items())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def ready():
    try:
        marker = load('instalado.json')
        signature = hashlib.sha256((CENTRAL/'manifesto.json').read_bytes()).hexdigest()
        return marker.get('manifest_sha256') == signature and model_ready()
    except (OSError, ValueError, TypeError):
        return False


def capabilities():
    config = load('config/pipeline.json')
    result = {'editor': config.get('editor', 'premiere'), 'hdr_sdr': False, 'encoder': 'libx265'}
    if result['editor'] == 'premiere':
        preset = load(config['cor']['preset_hdr_sdr'])
        base = ['ffmpeg', '-hide_banner', '-v', 'error']
        source = 'color=size=256x256:rate=1,format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc'
        try:
            smoke = subprocess.run(base+['-init_hw_device', 'vulkan', '-f', 'lavfi', '-i', source,
                '-frames:v', '1', '-vf', preset['filter'], '-f', 'null', '-'],
                capture_output=True, timeout=45)
            result['hdr_sdr'] = smoke.returncode == 0
            result['diagnostic'] = smoke.stderr.decode('utf-8', errors='replace')[-1500:] if smoke.returncode else ''
        except (OSError, subprocess.TimeoutExpired) as error:
            result['diagnostic'] = str(error)
        try:
            smoke = subprocess.run(base+['-f', 'lavfi', '-i', 'color=size=256x256:rate=1,format=p010le',
                '-frames:v', '1', *preset['gpu_encoder'], '-f', 'null', '-'], capture_output=True, timeout=30)
            if smoke.returncode == 0:
                result['encoder'] = 'hevc_nvenc'
        except (OSError, subprocess.TimeoutExpired):
            pass
    atomic_json(CENTRAL/'capacidades.json', result)
    print(f'Conversao HDR: {"disponivel" if result["hdr_sdr"] else "nao disponivel/nao aplicavel"}; codificador: {result["encoder"]}.', flush=True)
    return result


def relocate():
    from preparar_modelo import relocate_references
    status_path = CENTRAL/'caminhos.json'
    previous = (json.loads(status_path.read_text(encoding='utf-8'))['modelo']
                if status_path.exists() else load('manifesto.json')['model_origin'])
    current = str(CENTRAL/'prproj')
    if previous != current:
        relocate_references(Path(previous))
        status_path.write_text(json.dumps({'modelo': current}), encoding='utf-8')


def configure(verify_only=False, allow_sdr=False):
    relocate()
    from pipeline import preflight
    config = preflight()
    capability = capabilities()
    if config.get('editor', 'premiere') == 'premiere' and not capability['hdr_sdr'] and not verify_only and not allow_sdr:
        raise RuntimeError('Conversao HDR indisponivel. Instale o driver da GPU ou confirme explicitamente modo apenas SDR.')
    result = {'editor': config.get('editor', 'premiere'), 'python': sys.version.split()[0],
              'status': 'pronto' if model_ready() else 'modelo de transcricao ainda indisponivel',
              'capabilities': capability, 'sdr_only_accepted': allow_sdr,
              'manifest_sha256': hashlib.sha256((CENTRAL/'manifesto.json').read_bytes()).hexdigest()}
    complete = model_ready() and not verify_only
    atomic_json(CENTRAL/('instalado.json' if complete else 'verificacao.json'), result)
    (CENTRAL/'relatorios/RELATORIO-INSTALACAO.md').write_text(
        '# Instalacao\n\n- Python, bibliotecas e FFmpeg privados: conferidos.\n'
        '- Configuracao e modelo de projeto: conferidos.\n'
        f'- Modelo de transcricao: {"arquivos conferidos" if model_ready() else "pendente; execute a instalacao completa"}.\n'
        f'- Conversao HDR: {"disponivel" if capability["hdr_sdr"] else "indisponivel ou nao aplicavel; confira capacidades.json"}.\n'
        '- Nenhuma requisicao Gemini enviada nesta preparacao.\n'
        '- Abertura do projeto no aplicativo do editor ainda precisa ser validada neste computador.\n', encoding='utf-8')
    print('Configuracao pronta. Nenhuma requisicao Gemini enviada.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    for flag in ('integridade', 'bibliotecas', 'modelo', 'sem-download', 'configurar', 'relocalizar',
                 'capacidades', 'pronto', 'modelo-pronto', 'somente-verificar', 'permitir-sdr'):
        parser.add_argument('--'+flag, action='store_true')
    args = parser.parse_args()
    if args.integridade:
        integrity()
    if args.bibliotecas:
        libraries()
    if args.modelo:
        model()
    if args.sem_download:
        print('Verificacao sem download; modelo de transcricao ainda nao foi baixado.')
    if args.configurar:
        configure(args.somente_verificar, args.permitir_sdr)
    if args.relocalizar:
        relocate()
    if args.capacidades:
        capabilities()
    if args.pronto:
        return 0 if ready() else 42
    if args.modelo_pronto:
        return 0 if model_ready() else 42
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except LibraryVersionError as error:
        print(f'ERRO: {error}', file=sys.stderr)
        sys.exit(21)
    except (ImportError, OSError) as error:
        print(f'ERRO: {error}', file=sys.stderr)
        sys.exit(20)
