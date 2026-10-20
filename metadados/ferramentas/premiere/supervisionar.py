"""Supervisor do BAT: ESC interrompe somente seu worker e descendentes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from controle_execucao import read_control
from console_pipeline import say
from ambiente_online import logs_folder

CLIENTS = Path(os.environ.get('PREEDITOR_WORKSPACE_ROOT') or os.environ.get('PREEDITOR_CLIENTES', str(ROOT.parents[2])))


def cleanup(state, pid, clients=CLIENTS):
    """Depois da parada confirmada: limpar novos intermediários e lock próprio."""
    raw = state.get('edition')
    if not raw:
        return 0
    edition = Path(raw).resolve()
    try:
        relative = edition.relative_to(clients.resolve())
    except ValueError:
        raise ValueError('Pasta de interrupção fora de CLIENTES.') from None
    if len(relative.parts) != 2 or relative.parts[0].casefold() == 'metadados':
        raise ValueError('Pasta de interrupção não é uma edição de cliente.')
    removed = 0
    allowed = [edition/'metadados', edition/'premiere'/'audio', edition/'CapCut'/'audio']
    if any(not folder.resolve().is_relative_to(edition) for folder in allowed):
        raise ValueError('Pasta de intermediários redirecionada para fora da edição; limpeza recusada.')
    for filename in state.get('pending', []):
        path = Path(filename).resolve()
        if not any(path.is_relative_to(folder.resolve()) for folder in allowed):
            raise ValueError('Intermediário fora das pastas permitidas; limpeza recusada.')
        if path.is_file():
            path.unlink()
            removed += 1
    lock = edition/'metadados'/'processamento.lock'
    if lock.is_file() and lock.read_text(encoding='utf-8').startswith(f'PID {pid} —'):
        lock.unlink()
    from exportar_premiere import report
    (edition/'metadados').mkdir(exist_ok=True)
    report(edition/'metadados', '- Interrompido pelo usuário (ESC/Ctrl+C). Originais, versões anteriores e caches concluídos preservados.\n'
           f'- {removed} arquivo(s) intermediário(s) incompleto(s) desta execução removido(s); podem ser recriados.',
           title='Execução interrompida', section='## Pré-edição integrada')
    return removed


def stop_tree(process):
    if process.poll() is not None:
        return
    result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Não foi possível confirmar a parada. Não reinicie antes de fechar esta execução.') from None
    if result.returncode and process.poll() is None:
        raise RuntimeError('Não foi possível interromper o processamento.')


def wait_for_escape(process, path, key_available, read_key):
    cancelled = False
    while process.poll() is None:
        state = read_control(path)
        # Durante perguntas, só o worker lê o teclado.
        if state.get('phase') in {'processing', 'publishing'} and key_available():
            if read_key() in {'\x1b', '\x03'}:
                cancelled = True
        if cancelled and state.get('phase') != 'publishing':
            return True
        time.sleep(.1)
    return cancelled


def main():
    if os.name != 'nt':
        return subprocess.call([sys.executable, '-u', str(ROOT/'pipeline.py'), *sys.argv[1:]])
    import msvcrt
    logs = logs_folder(ROOT.parents[1])
    logs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='controle-', dir=logs) as folder:
        path = Path(folder)/'controle.json'
        path.write_text(json.dumps({'phase': 'selecting', 'pending': []}), encoding='utf-8')
        env = {**os.environ, 'PREEDITOR_CONTROLE': str(path)}
        process = subprocess.Popen([sys.executable, '-u', str(ROOT/'pipeline.py'), *sys.argv[1:]],
                                   env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        cancelled = False
        try:
            cancelled = wait_for_escape(process, path, msvcrt.kbhit, msvcrt.getwch)
        except KeyboardInterrupt:
            cancelled = True
        if cancelled:
            say('\nInterrompendo a pré-edição...', 'yellow')
            # Não interromper a curta gravação transacional de um projeto pronto.
            while process.poll() is None and read_control(path).get('phase') == 'publishing':
                time.sleep(.1)
            stop_tree(process)
            removed = cleanup(read_control(path), process.pid)
            from exportar_premiere import report
            report(logs, '- Lote interrompido pelo usuário (ESC/Ctrl+C); próximas edições não foram iniciadas.\n'
                   f'- {removed} intermediário(s) incompleto(s) removido(s), recuperável(is) pela reexecução. Originais e projetos concluídos preservados.',
                   title='Interrupção', section='## Execuções')
            say(f'Interrompido. Originais e projetos anteriores preservados; {removed} intermediário(s) incompleto(s) removido(s).', 'yellow')
            return 130
        return process.wait()


if __name__ == '__main__':
    raise SystemExit(main())
