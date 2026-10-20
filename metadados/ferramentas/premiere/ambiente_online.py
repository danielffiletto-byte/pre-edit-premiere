"""Rotas opcionais para separar fontes temporárias e dados persistentes."""
import os
from pathlib import Path


def logs_folder(central):
    return Path(os.environ.get('PREEDITOR_METADATA_ROOT', str(central)))/'relatorios'


def model_path(central, config):
    return Path(os.environ.get('PREEDITOR_MODEL_PATH', str(central/config['modelo'])))


def model_cache(default):
    return Path(os.environ.get('PREEDITOR_MODEL_CACHE', str(default)))


def work_edition(edition, clients):
    root = os.environ.get('PREEDITOR_WORKSPACE_ROOT')
    if not root:
        return edition
    relative = edition.resolve().relative_to(clients.resolve())
    if len(relative.parts) != 2:
        raise ValueError('Edição fora da estrutura Cliente/Edição.')
    output = Path(root).resolve()/relative
    if not output.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('Pasta de trabalho fora de Metadados.')
    return output
