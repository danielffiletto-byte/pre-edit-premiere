"""Entrada remota: prepara recursos persistentes e executa o motor temporário."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

SOURCE = Path(__file__).resolve().parents[1]/'metadados'
sys.path.insert(0, str(SOURCE/'ferramentas/premiere'))
sys.path.insert(0, str(SOURCE/'ferramentas'))


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@contextmanager
def activity(label):
    stop = threading.Event()
    started = time.monotonic()
    print(label, flush=True)
    def notify():
        while not stop.wait(10):
            elapsed = int(time.monotonic()-started)
            print(f'  Em atividade | {elapsed//60:02d}:{elapsed%60:02d}', flush=True)
    worker = threading.Thread(target=notify, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=1)


def safe_folder(path, base):
    base, path = base.resolve(), path.absolute()
    if not path.resolve().is_relative_to(base):
        raise RuntimeError('Pasta técnica fora de Metadados.')
    for candidate in (path, *path.parents):
        if candidate == base.parent:
            break
        if candidate.exists() and (candidate.is_symlink() or candidate.is_junction()):
            raise RuntimeError('Link/junction inesperado em pasta técnica.')
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_references(metadata):
    original = SOURCE/'prproj'
    files = sorted(p for p in original.rglob('*') if p.is_file())
    key = hashlib.sha256(json.dumps([(p.relative_to(original).as_posix(), digest(p)) for p in files]).encode()).hexdigest()[:20]
    destination = safe_folder(metadata/'Referencias'/key/'prproj', metadata)
    marker = destination.parent/'conferido.json'
    if marker.exists():
        saved = json.loads(marker.read_text(encoding='utf-8'))
        if not saved or any(not (destination/name).is_file() or digest(destination/name) != value for name, value in saved.items()):
            raise RuntimeError('Referência local alterada; preservada para revisão, sem sobrescrever.')
    else:
        if any(destination.iterdir()):
            raise RuntimeError('Referência incompleta anterior; preserve e renomeie essa pasta antes de repetir.')
        for file in files:
            target = destination/file.relative_to(original)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
        from preparar_modelo import relocate_references
        manifest = json.loads((SOURCE/'manifesto.json').read_text(encoding='utf-8'))
        relocate_references(Path(manifest['model_origin']), target_folder=destination)
        marker.write_text(json.dumps({p.relative_to(destination).as_posix(): digest(p) for p in destination.rglob('*') if p.is_file()}, indent=2), encoding='utf-8')
    return destination/'modelo-base.prproj'


def check_libraries():
    expected = json.loads((SOURCE/'manifesto.json').read_text(encoding='utf-8'))['dependencies']
    for name, version in expected.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise RuntimeError(f'Biblioteca ausente: {name}; conferir o runtime, não instalar VC++ para corrigir isso.') from None
        if actual != version:
            raise RuntimeError(f'Biblioteca incompatível: {name}; ambiente não foi substituído.')
    try:
        import av
        import ctranslate2
        import onnxruntime
        import tokenizers
    except (ImportError, OSError) as error:
        raise ImportError('DLL do ambiente indisponível; verificar Microsoft Visual C++ Runtime.') from error


def prepare_model(metadata, verify=False):
    from faster_whisper.utils import download_model
    cache = safe_folder(metadata/'Modelos'/'Whisper', metadata)
    revision = json.loads((SOURCE/'manifesto.json').read_text(encoding='utf-8'))['whisper_revision']
    options = dict(cache_dir=str(cache), revision=revision)
    names = ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')
    try:
        snapshot_path = Path(download_model('medium', local_files_only=True, **options))
        if not all((snapshot_path/name).is_file() and (snapshot_path/name).stat().st_size > 0 for name in names):
            raise RuntimeError('Cache local incompleto.')
        print('Modelo de transcrição no cache; sem novo download.', flush=True)
    except Exception:
        if verify:
            raise RuntimeError('Modelo ainda não instalado; execute o BAT sem --verificar.') from None
        if shutil.disk_usage(metadata).free < 4*1024**3:
            raise RuntimeError('Reserve 4 GB livres para preparar o modelo de transcrição.')
        with activity('Baixando modelo de transcrição (~1,5 GB; apenas no primeiro uso)...'):
            download_model('medium', **options)
    ref = cache/'models--Systran--faster-whisper-medium/refs/main'
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(revision, encoding='utf-8')
    snapshot = cache/'models--Systran--faster-whisper-medium/snapshots'/revision
    if not all((snapshot/name).is_file() for name in names):
        raise RuntimeError('Download do modelo incompleto; execute novamente para retomar o cache.')
    sizes = {name: (snapshot/name).stat().st_size for name in names}
    if any(value <= 0 for value in sizes.values()):
        raise RuntimeError('Modelo de transcrição incompleto.')
    marker = metadata/'Modelos'/'whisper-pronto.json'
    saved = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
    if saved.get('revision') != revision or saved.get('files') != sizes:
        from faster_whisper import WhisperModel
        with activity('Conferindo modelo local de transcrição...'):
            WhisperModel('medium', device='cpu', compute_type='int8', download_root=str(cache), local_files_only=True)
        marker.write_text(json.dumps({'revision': revision, 'files': sizes}), encoding='utf-8')
    return cache


def sanitize_metadata(metadata):
    """Preserva decisões/caches, não o briefing ou o objeto de configuração do motor."""
    work = metadata/'Trabalhos'
    if not work.exists():
        return
    for path in work.rglob('plano-nativo-v*.json'):
        data = json.loads(path.read_text(encoding='utf-8'))
        if 'input' in data:
            data.pop('input')
            temp = path.with_suffix('.json.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--clients', type=Path, required=True)
    parser.add_argument('--preparar', action='store_true')
    parser.add_argument('--verificar', action='store_true')
    args = parser.parse_args()
    metadata = args.metadata.resolve()
    metadata.mkdir(parents=True, exist_ok=True)
    work = safe_folder(metadata/'Trabalhos', metadata)
    safe_folder(metadata/'relatorios', metadata)
    check_libraries()
    os.environ.update(PREEDITOR_METADATA_ROOT=str(metadata), PREEDITOR_CLIENTES=str(args.clients.resolve()),
                      PREEDITOR_WORKSPACE_ROOT=str(work),
                      PREEDITOR_MODEL_PATH=str(prepare_references(metadata)))
    if args.preparar:
        # Suporte HDR deve manter o preset aprovado, nunca substituir por outro look.
        preset = json.loads((SOURCE/'config/hdr-sdr.json').read_text(encoding='utf-8'))
        marker = metadata/'Runtime'/'hdr-conferido.json'
        signature = hashlib.sha256(preset['filter'].encode()).hexdigest()
        saved = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
        if saved.get('preset') != signature or not saved.get('available'):
            video = 'color=size=256x256:rate=1,format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc'
            result = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-init_hw_device', 'vulkan', '-f', 'lavfi', '-i', video,
                                     '-frames:v', '1', '-vf', preset['filter'], '-f', 'null', '-'], capture_output=True, timeout=45)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({'preset': signature, 'available': result.returncode == 0}), encoding='utf-8')
            if result.returncode:
                print('AVISO: conversão HDR indisponível neste driver. Nenhuma cor alternativa será aplicada.', flush=True)
        os.environ['PREEDITOR_MODEL_CACHE'] = str(prepare_model(metadata, args.verificar))
        print('Ambiente preparado. Nenhuma API de títulos foi chamada.', flush=True)
        return 0
    os.environ['PREEDITOR_MODEL_CACHE'] = str(metadata/'Modelos'/'Whisper')
    try:
        return subprocess.call([sys.executable, '-s', '-B', '-u', str(SOURCE/'ferramentas/premiere/supervisionar.py'),
                                *(['--verificar'] if args.verificar else [])])
    finally:
        sanitize_metadata(metadata)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ImportError:
        print('ERRO: bibliotecas/DLLs indisponíveis; conferir o ambiente Windows.', file=sys.stderr)
        raise SystemExit(20)
    except OSError as error:
        if getattr(error,'winerror',None) in (126,127,1114):
            print('ERRO: DLL do ambiente indisponível.',file=sys.stderr)
            raise SystemExit(20)
        print('ERRO local: confira espaço, permissões e integridade dos arquivos.',file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(f'ERRO: {error}', file=sys.stderr)
        raise SystemExit(1)
