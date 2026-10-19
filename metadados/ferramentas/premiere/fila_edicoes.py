"""Varredura e escolha humana; não abre mídias nem cria pastas nas edições."""
import re
import os
import sys
import unicodedata

from console_pipeline import say


class SelectionCancelled(Exception):
    pass


def read_console(prompt):
    if os.name != 'nt' or not sys.stdin.isatty():
        return input(prompt)
    import msvcrt
    print(prompt, end='', flush=True)
    chars = []
    while True:
        key = msvcrt.getwch()
        if key in {'\x1b', '\x03'}:
            print()
            raise SelectionCancelled()
        if key in {'\x00', '\xe0'}:
            msvcrt.getwch()  # Ignora teclas de função/setas, sem inserir códigos.
        elif key in {'\r', '\n'}:
            print()
            return ''.join(chars)
        elif key == '\b':
            if chars:
                chars.pop()
                print('\b \b', end='', flush=True)
        elif key.isprintable():
            chars.append(key)
            print(key, end='', flush=True)


def answer(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                    if not unicodedata.combining(c)).strip().strip('.!?').strip()
    if not value or value in {'n', 'no', 'nao', 'negativo', 'nem', 'nop', 'nope'}:
        return False
    if value in {'sim', 'si', 'yes', 'y', 'smi'} or re.fullmatch('s+', value):
        return True
    return None


def ask(prompt, read):
    while True:
        try:
            value = answer(read(prompt))
        except (EOFError, KeyboardInterrupt):
            raise SelectionCancelled() from None
        if value is not None:
            return value
        say('Responda sim ou não. ENTER = não.', 'yellow')


def existing_projects(edition, editor='premiere'):
    if editor == 'capcut':
        return list((edition/'CapCut').glob('*/draft_content.json'))
    return [p for folder in (edition, edition/'premiere') if folder.is_dir()
            for p in folder.iterdir() if p.is_file() and p.suffix.casefold() == '.prproj']


def indices(value, count):
    value = re.sub(r'\be\b', ' ', value.casefold()).strip()
    if not re.fullmatch(r'[0-9]+(?:[\s,;]+[0-9]+)*', value):
        raise ValueError('Use números separados por espaço ou vírgula; ENTER vazio termina.')
    selected = {int(n) for n in re.findall(r'[0-9]+', value)}
    if not selected or min(selected) < 1 or max(selected) > count:
        raise ValueError(f'Escolha apenas números de 1 a {count}.')
    return selected


def select_numbers(count, read):
    selected = set()
    say('Quais edições refazer? Ex.: 1, 3 e 4; ou um número por linha. ENTER vazio termina.')
    while True:
        try:
            raw = read('> ')
        except (EOFError, KeyboardInterrupt):
            raise SelectionCancelled() from None
        if not raw.strip():
            return selected
        try:
            selected.update(indices(raw, count))
        except ValueError as error:
            say(str(error), 'yellow')
            continue
        say('Selecionadas: ' + ', '.join(map(str, sorted(selected))), 'yellow')


def choose_jobs(jobs, read=None, editor='premiere'):
    read = read or read_console
    ready = [(edition, videos) for edition, videos in jobs if existing_projects(edition, editor)]
    ready_paths = {edition for edition, _ in ready}
    say(f'Varredura: {len(jobs)-len(ready)} novas | {len(ready)} com projeto.')
    if not ready:
        return [(edition, videos, False) for edition, videos in jobs]
    for i, (edition, _) in enumerate(ready, 1):
        say(f'{i} - {edition.parent.name} / {edition.name}', 'yellow')
    wants_redo = ask('Refazer alguma edição que já tem projeto? [s/N]: ', read)
    while True:
        if wants_redo:
            selected = {1} if len(ready) == 1 else select_numbers(len(ready), read)
            if selected:
                paths = {ready[i-1][0] for i in selected}
                return [(edition, videos, edition in paths) for edition, videos in jobs
                        if edition not in ready_paths or edition in paths]
        wants_redo = ask('Confirmação: quer refazer alguma edição pronta? [s/N; ENTER = só novas]: ', read)
        if not wants_redo:
            return [(edition, videos, False) for edition, videos in jobs if edition not in ready_paths]
