"""Estado mínimo do supervisor ESC; sem chave ou conteúdo do vídeo."""
from contextlib import contextmanager
import json
import os
from pathlib import Path


def read_control(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def control(**changes):
    filename = os.environ.get('PREEDITOR_CONTROLE')
    if not filename:
        return
    path = Path(filename)
    data = {**read_control(path), **changes}
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


@contextmanager
def pending_files(paths):
    """Só arquivos novos deste trabalho podem ser removidos ao interromper."""
    created = [Path(p).resolve() for p in paths if not Path(p).exists()]
    filename = os.environ.get('PREEDITOR_CONTROLE')
    before = read_control(filename).get('pending', []) if filename else []
    control(pending=before + [str(p) for p in created])
    try:
        yield
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    finally:
        control(pending=before)
