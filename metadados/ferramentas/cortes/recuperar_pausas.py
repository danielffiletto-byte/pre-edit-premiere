"""Restaura integralmente pausas escolhidas; renderiza do original, usando o historico."""
from __future__ import annotations

import argparse
import io
from pathlib import Path
import re
import sys
import traceback

import cortar_silencios as cutter
from historico_edicao import (parse_ids, prepare_record, read_history, cut_reading,
                              select_pauses, load_transcript, pause_list, update_pause_list)
from console_pipeline import say, summary, paint


def choose_version(records, requested=None):
    if not records:
        raise ValueError('Nenhum plano encontrado no RELATORIO.md. Execute o cortador primeiro.')
    if requested is not None:
        token = str(requested).strip()
        number = re.fullmatch(r'v?(\d+)', token, re.IGNORECASE)
        matches = [r for r in records if (number and r['version'] == int(number.group(1)))
                   or token.casefold() == r['output'].casefold()]
        if len(matches) != 1:
            raise ValueError('Versao inexistente ou ambigua no historico.')
        return matches[0]
    latest = records[-1]
    if len(records) == 1:
        return latest
    print('Versões: ' + ', '.join(f'v{record["version"]:03}' for record in records))
    choice = input(f'Versão para revisar [v{latest["version"]:03}]: ').strip()
    return choose_version(records, choice) if choice else latest


def recover(folder, base, ids, debug=None, show_header=True):
    selected = set(ids)
    active = {cut['id'] for cut in prepare_record(base)['removed']}
    if not selected or selected - active:
        raise ValueError('Selecione somente numeros de cortes ativos da versao escolhida.')
    return cutter.run(folder, debug=debug, recovery={'base': base, 'selected': sorted(selected)}, show_header=show_header)


def validate_base(folder, base):
    path = folder / base['output']
    if not path.is_file():
        return  # O original + plano ainda permitem reconstruir uma versao cujo MP4 foi removido.
    expected_hash = base.get('output_sha256')
    if expected_hash:
        if cutter.file_hash(path) != expected_hash:
            raise ValueError('A versao escolhida foi alterada fora da pipeline. Seus timestamps precisam ser revistos.')
    else:
        # Relatorios antigos nao guardavam hash do MP4; conferir ao menos a duracao.
        expected = base.get('planned_output_duration')
        if expected is None and base.get('time_map'):
            expected = base['time_map'][-1]['output_end']
        actual = cutter.media_info(path)
        if expected is not None and abs(float(actual['format']['duration'])-float(expected)) > .15:
            raise ValueError('A duracao do MP4 nao corresponde ao mapa da versao no relatorio. Nao e seguro usar esses tempos.')


def show_cuts(record, transcript=None):
    print(f'\nVersão em revisão: {record["output"]}\n')
    if not transcript:
        print('Transcrição indisponível; exibindo somente os cortes.\n')
    text = cut_reading(record, transcript)
    print(re.sub(r'\[CORTE \d+\]', lambda match: paint(match.group(), 'yellow'), text))
    if record.get('restored_pause_ids'):
        print('\nJá recuperadas: ' + ', '.join(map(str, record['restored_pause_ids'])))
    print('')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=cutter.ROOT)
    parser.add_argument('--version', help='v004, 4 ou nome completo do MP4')
    parser.add_argument('--pauses', nargs='+', help='IDs separados por espaco, virgula ou e; omitir para modo interativo')
    args = parser.parse_args()
    folder = args.folder.resolve()
    if not folder.is_dir():
        parser.error('Pasta inexistente.')
    debug = io.StringIO()
    try:
        say('recuperador de pausas por Daniel Fileto')
        print('')
        records = read_history(folder)
        # Apenas uma edicao por pasta: evita escolher versoes de outro original.
        originals = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in cutter.EXTENSIONS
                     and not cutter.is_version(p)]
        if len(originals) != 1:
            raise ValueError('Mantenha exatamente um video original nesta pasta.')
        original = originals[0]
        records = [r for r in records if r['source'].get('name', '').casefold() == original.name.casefold()]
        base = choose_version(records, args.version)
        if cutter.file_hash(original) != base['source'].get('sha256'):
            raise ValueError('O original nao corresponde ao historico escolhido.')
        transcript = load_transcript(folder, original, base['source']['sha256'])
        base = prepare_record(base, transcript)
        validate_base(folder, base)
        if not base['removed']:
            summary('NENHUMA PAUSA PARA RECUPERAR', ['Todas as pausas desta versão já estão completas.'])
            return 0
        show_cuts(base, transcript)
        # Compatibilidade: torna consultavel a lista de uma versao antiga que so tinha o mapa tecnico.
        if not update_pause_list(folder, base, transcript):
            cutter.report(folder, pause_list(base, transcript),
                          title=f'Lista da v{base["version"]:03}', section='## Histórico de versões')
        ids = parse_ids(' '.join(args.pauses)) if args.pauses is not None else select_pauses(base)
        if not ids:
            print('Nenhuma pausa selecionada. Nenhuma nova versão gerada.')
            return 0
        recover(folder, base, ids, debug, show_header=False)
        return 0
    except (Exception, KeyboardInterrupt, EOFError) as error:
        detail = f'{type(error).__name__}: {error}'
        diagnostics = traceback.format_exc() + '\n' + debug.getvalue()[-10000:]
        cutter.report(folder, '- Status: ERRO\n- Etapa: Recuperador\n- Detalhe: ' + detail
                      + '\n\n<details>\n<summary>Diagnóstico técnico</summary>\n\n```text\n'
                      + diagnostics + '\n```\n\n</details>', section='## Recuperação de pausas')
        summary('RECUPERAÇÃO INTERROMPIDA', [str(error) or 'Execução interrompida.', 'Relatório atualizado.'], 'red')
        return 1


if __name__ == '__main__':
    sys.exit(main())
