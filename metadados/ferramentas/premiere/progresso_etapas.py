"""Progresso visual estimado; conclusão depende das etapas reais, sem ETA."""
import sys
import threading
import time
from console_pipeline import ANSI, paint, elapsed_text


class StageProgress:
    def __init__(self, total=5, stream=None):
        self.total = total
        self.stream = stream or sys.stdout
        self.completed = 0
        self.label = 'Preparando'
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.lines = False
        self.worker = None
        self.failed = False
        self.stage_started = self.started
        self.active = False

    def fraction(self, now=None):
        now = time.monotonic() if now is None else now
        base = self.completed / self.total
        if not self.active or self.completed == self.total:
            return base
        # Um ponto percentual a cada quatro segundos. Não alcança o marco
        # da etapa seguinte, nem aumenta a contagem de etapas concluídas.
        visual = int(max(0, now-self.stage_started) / 4) * .01
        return base + min(visual, .2, .95/self.total)

    def draw(self):
        with self.lock:
            now = time.monotonic()
            fraction = self.fraction(now)
            percent = min(99, int(fraction*100 + 1e-8)) if self.completed < self.total else 100
            filled = min(29, int(30*fraction)) if self.completed < self.total else 30
            bar = '#' * filled + '-' * (30-filled)
            spinner = '|/-\\'[int(now - self.started) % 4]
            status = f'{spinner} {self.label} | {elapsed_text(now-self.started)}'
            progress = f'[{bar}] {percent}%'
            if self.completed < self.total:
                progress += ' estimado'
            progress += f' — {self.completed}/{self.total} etapas concluídas'
            if self.stream.isatty() and ANSI:
                if self.lines:
                    self.stream.write('\x1b[2A')
                self.stream.write('\r\x1b[2K' + paint(progress, 'red' if self.failed else 'cyan', self.stream) + '\n')
                self.stream.write('\r\x1b[2K' + status + '\n')
                self.lines = True
            else:
                self.stream.write(f'{progress} | {status}\n')
            self.stream.flush()

    def _animate(self):
        while not self.stop.wait(1 if self.stream.isatty() else 15):
            self.draw()

    def __enter__(self):
        self.draw()
        self.worker = threading.Thread(target=self._animate, daemon=True)
        self.worker.start()
        return self

    def stage(self, label):
        with self.lock:
            if not self.active:
                self.stage_started = time.monotonic()
                self.active = True
            self.label = label
            self.draw()

    def done(self):
        with self.lock:
            self.completed = min(self.total, self.completed + 1)
            self.active = False
            self.draw()

    def __exit__(self, kind, error, tb):
        self.stop.set()
        self.worker.join(timeout=2)
        self.failed = kind is not None
        self.label = 'Interrompido — consulte o relatório' if kind else self.label
        self.draw()
        return False
