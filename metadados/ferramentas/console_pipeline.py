"""Console compartilhado, sem dependencias: cores e uma linha de progresso."""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
import threading
import time

COLORS = {'cyan': '96', 'green': '92', 'yellow': '93', 'red': '91', 'muted': '90'}


def enable_ansi():
    if os.name != 'nt':
        return True
    try:
        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.restype = ctypes.c_void_p
        kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))
                    and kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


ANSI = enable_ansi()


def paint(text, color='cyan', stream=None):
    stream = stream or sys.stdout
    if ANSI and stream.isatty() and (os.environ.get('PIPELINE_COLOR') == '1' or 'NO_COLOR' not in os.environ):
        return f'\033[{COLORS[color]}m{text}\033[0m'
    return text


def say(text, color='cyan'):
    print(paint(text, color), flush=True)


def section(title, color='cyan'):
    width = min(68, shutil.get_terminal_size((80, 24)).columns - 2)
    say('\n' + '-' * max(20, width), color)
    say('  ' + title, color)


def summary(title, lines, color='green'):
    section(title, color)
    for line in lines:
        print('  ' + line, flush=True)
    say('-' * min(68, max(20, shutil.get_terminal_size((80, 24)).columns - 2)), color)


def elapsed_text(value):
    value = max(0, int(value))
    minutes, seconds = divmod(value, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}'


class LiveProgress:
    """TTY: reescreve uma linha. Logs: imprime apenas marcos de 10%."""

    def __init__(self, title, stream=None, inline=False):
        self.title = title
        self.inline = inline
        self.stream = stream or sys.stdout
        self.interactive = self.stream.isatty()
        self.started = time.monotonic()
        self.fraction = None
        self.detail = 'em atividade'
        self._last_bucket = -1
        self._last_width = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker = None

    def __enter__(self):
        if not self.inline:
            section(self.title)
        self._draw(force=True)
        if self.interactive:
            self._worker = threading.Thread(target=self._tick, daemon=True)
            self._worker.start()
        return self

    def _tick(self):
        while not self._stop.wait(3):
            with self._lock:
                self._draw()

    def update(self, fraction=None, detail=None):
        with self._lock:
            if fraction is not None:
                self.fraction = max(self.fraction or 0, min(0.999, fraction))
            if detail:
                self.detail = detail
            if not self.interactive:
                self._draw()

    def _draw(self, force=False, finished=False, failed=False):
        elapsed = time.monotonic() - self.started
        width = max(24, shutil.get_terminal_size((80, 24)).columns - 1)
        size = min(24, max(8, width - 60))
        if self.fraction is None and not finished:
            spinner = '|/-\\'[int(elapsed / 3) % 4]
            line = f'  {spinner} {self.detail} | decorrido {elapsed_text(elapsed)}'
            bucket = -1
        else:
            fraction = 1 if finished and not failed else (self.fraction or 0)
            percent = int(fraction * 100)
            filled = int(size * fraction)
            # ASCII facilita compatibilidade com fontes antigas do console.
            bar = '#' * filled + '-' * (size - filled)
            line = f'  [{bar}] {percent:3}% | decorrido {elapsed_text(elapsed)}'
            if 0.02 < fraction < 1:
                line += f' | falta ~{elapsed_text(elapsed * (1-fraction)/fraction)}'
            if finished:
                line += ' | ERRO' if failed else ' | OK'
            elif self.detail and self.detail != 'em atividade':
                line += ' | ' + self.detail
            bucket = percent // 10
        color = 'red' if failed else ('green' if finished else 'cyan')
        if self.inline:
            fraction = 1 if finished and not failed else self.fraction
            percent = f'{int(fraction*100):3}%' if fraction is not None else ' --%'
            suffix = f' {percent} | {elapsed_text(elapsed)}'
            if finished:
                suffix += ' | ERRO' if failed else ' | OK'
            prefix = self.title + ' '
            size = min(16, max(4, width - len(prefix) - len(suffix) - 2))
            if fraction is None:
                position = int(elapsed / 3) % size
                bar = '-' * position + '#' + '-' * (size-position-1)
            else:
                filled = int(size * fraction)
                bar = '#' * filled + '-' * (size-filled)
            line = prefix + '[' + bar + ']' + suffix
        if self.interactive:
            line = line[:width]
            padding = ' ' * max(0, self._last_width - len(line))
            self.stream.write('\r' + paint(line + padding, color, self.stream))
            self._last_width = len(line)
            if finished:
                self.stream.write('\n')
            self.stream.flush()
        elif finished or (not self.inline and (force or bucket > self._last_bucket)):
            self.stream.write(line + '\n')
            self.stream.flush()
        self._last_bucket = max(self._last_bucket, bucket)

    def __exit__(self, kind, error, tb):
        self._stop.set()
        if self._worker:
            self._worker.join()
        with self._lock:
            self._draw(force=True, finished=True, failed=kind is not None)
        return False


if __name__ == '__main__':
    section('PIPELINE DE EDICAO | Demonstracao')
    with LiveProgress('Renderizando video') as progress:
        for percent in range(0, 101, 10):
            progress.update(percent / 100)
            time.sleep(0.3)
    summary('CONCLUIDO', ['Exemplo visual; nenhum video foi processado.', 'Relatorio salvo ao final de cada etapa.'])
