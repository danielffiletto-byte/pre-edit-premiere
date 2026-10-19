"""Gemini escolhe palavras; Python valida IDs e calcula tempos/cortes."""
from __future__ import annotations
import copy
from datetime import datetime
from difflib import SequenceMatcher
from fractions import Fraction
import hashlib
import json
import re
import unicodedata
from pathlib import Path

from gemini_cliente import MODEL, generate, selected_model, client_options
from exportar_premiere import ROOT, file_hash, quantize
from legendar_premiere import atomic_json

SCHEMA = {'type': 'object', 'required': ['repeticoes', 'titulos', 'visuais'], 'properties': {
    'repeticoes': {'type': 'array', 'items': {'type': 'object',
        'required': ['inicio_descartar', 'fim_descartar', 'inicio_manter', 'fim_manter', 'confianca', 'motivo'],
        'properties': {**{k: {'type': 'string'} for k in ['inicio_descartar', 'fim_descartar', 'inicio_manter', 'fim_manter', 'motivo']},
                       'confianca': {'type': 'number', 'minimum': 0, 'maximum': 1}}}},
    'titulos': {'type': 'array', 'items': {'type': 'object', 'required': ['palavras', 'categoria', 'lista'],
        'properties': {'palavras': {'type': 'array', 'minItems': 1, 'maxItems': 6, 'items': {'type': 'string'}},
                       'categoria': {'type': 'string', 'enum': ['neutro', 'positivo', 'negativo', 'alerta']},
                       'lista': {'type': 'string'}}}},
    'visuais': {'type': 'array', 'items': {'type': 'object', 'required': ['ancora', 'tipo', 'descricao'],
        'properties': {k: {'type': 'string'} for k in ['ancora', 'tipo', 'descricao']}}}}}


def schema_for(words):
    """IDs válidos são opções da resposta, não apenas uma orientação no prompt."""
    if not words:
        raise ValueError('Transcrição sem palavras com timestamps; análise não enviada.')
    schema = copy.deepcopy(SCHEMA)
    schema['$defs'] = {'wordId': {'type': 'string', 'enum': [w['id'] for w in words],
                                  'description': 'Copie um ID existente da transcrição. Não escreva a palavra nem invente IDs.'}}
    title = schema['properties']['titulos']['items']['properties']['palavras']
    title['items'] = {'$ref': '#/$defs/wordId'}
    title['description'] = 'De 1 a 6 IDs distintos de palavras faladas, em ordem de ocorrência; não é o texto do título.'
    for key in ('inicio_descartar', 'fim_descartar', 'inicio_manter', 'fim_manter'):
        schema['properties']['repeticoes']['items']['properties'][key] = {'$ref': '#/$defs/wordId'}
    schema['properties']['visuais']['items']['properties']['ancora'] = {'$ref': '#/$defs/wordId'}
    return schema


def normalized(word):
    return re.sub(r'[^a-z0-9]', '', ''.join(c for c in unicodedata.normalize('NFKD', word.casefold())
                                           if not unicodedata.combining(c)))


def word_catalog(transcripts):
    words = []
    for source_index, transcript in enumerate(transcripts):
        number = 0
        for segment in transcript['segments']:
            for word in segment.get('words', []):
                words.append({'id': f's{source_index+1}w{number}', 'source': source_index,
                              'word': word['word'].strip(), 'start': float(word['start']),
                              'end': float(word['end'])})
                number += 1
    return words


def preserve_negations(result, words):
    """Reinsere apenas IDs de negações já faladas dentro do recorte escolhido.

    Não adivinha IDs inválidos nem aceita títulos longos/fora de ordem. A validação
    completa continua obrigatória depois deste ajuste determinístico.
    """
    adjusted = copy.deepcopy(result)
    changes = []
    lookup = {w['id']: i for i, w in enumerate(words)}
    titles = adjusted.get('titulos', []) if isinstance(adjusted, dict) else []
    if not isinstance(titles, list):
        return adjusted, changes
    for number, title in enumerate(titles, 1):
        ids = title.get('palavras') if isinstance(title, dict) else None
        if not isinstance(ids, list) or not 1 <= len(ids) <= 6 or any(not isinstance(i, str) or i not in lookup for i in ids):
            continue
        indices = [lookup[i] for i in ids]
        if indices != sorted(set(indices)) or indices[-1]-indices[0] > 15:
            continue
        extra = [i for i in range(indices[0], indices[-1]+1)
                 if i not in indices and normalized(words[i]['word']) in {'nao', 'nunca', 'jamais', 'sem'}]
        if not extra or len(indices)+len(extra) > 6:
            continue
        new_ids = [words[i]['id'] for i in sorted(indices+extra)]
        title['palavras'] = new_ids
        changes.append({'titulo': number, 'motivo': 'Negação falada reinserida para preservar o sentido',
                        'ids_recebidos': ids, 'ids_aplicados': new_ids})
    return adjusted, changes


def validate_response(result, words):
    if not isinstance(result, dict) or set(result) != {'repeticoes', 'titulos', 'visuais'}:
        raise ValueError('Análise fora do contrato editorial.')
    if any(not isinstance(result[k], list) for k in result):
        raise ValueError('Análise editorial deve conter listas.')
    lookup = {w['id']: (i, w) for i, w in enumerate(words)}
    accepted, review, discarded = [], [], set()
    for repetition in result['repeticoes']:
        keys = ['inicio_descartar', 'fim_descartar', 'inicio_manter', 'fim_manter']
        if any(repetition.get(k) not in lookup for k in keys):
            raise ValueError('Repetição com ID inexistente.')
        start, end, keep, keep_end = [lookup[repetition[k]][0] for k in keys]
        confidence = float(repetition.get('confianca', 0))
        if not 0 <= confidence <= 1 or not start <= end < keep <= keep_end or end + 1 != keep:
            raise ValueError('Repetição com ordem/limites inválidos.')
        before = [normalized(w['word']) for w in words[start:end+1]]
        after = [normalized(w['word']) for w in words[keep:keep_end+1]]
        match = SequenceMatcher(None, before, after, autojunk=False).find_longest_match()
        plausible = (match.size >= 2 and match.size / max(1, len(before)) >= .5
                     or (len(before) == 1 and before[0] == after[0]))
        local = words[keep]['source'] - words[start]['source'] in (0, 1) and keep-start <= 80
        if confidence < .95 or not plausible or not local:
            review.append(repetition)
            continue
        if any(index in discarded for index in range(keep, keep_end+1)):
            raise ValueError('Tentativa mantida conflita com corte anterior.')
        accepted.append(repetition)
        discarded.update(range(start, end+1))
    titles = []
    seen = set()
    for number, title in enumerate(result['titulos'], 1):
        if not isinstance(title, dict):
            raise ValueError(f'Título {number}: deve ser um objeto com palavras, categoria e lista.')
        ids = title.get('palavras', [])
        if not isinstance(ids, list):
            raise ValueError(f'Título {number}: palavras deve ser uma lista de IDs, não texto.')
        if not 1 <= len(ids) <= 6:
            raise ValueError(f'Título {number}: recebeu {len(ids)} IDs; permitido de 1 a 6.')
        invalid = [ident for ident in ids if not isinstance(ident, str) or ident not in lookup]
        if invalid:
            raise ValueError(f'Título {number}: IDs inexistentes no catálogo: {str(invalid)[:180]}.')
        indices = [lookup[ident][0] for ident in ids]
        if indices != sorted(set(indices)) or indices[-1] - indices[0] > 15:
            raise ValueError(f'Título {number}: IDs fora da ordem, duplicados ou espalhados por frases diferentes: {ids}.')
        if any(i in discarded for i in indices):
            continue
        category = title.get('categoria')
        if category not in ('neutro', 'positivo', 'negativo', 'alerta'):
            raise ValueError('Categoria de título inválida.')
        span = {normalized(w['word']) for w in words[indices[0]:indices[-1]+1]}
        chosen = {normalized(words[i]['word']) for i in indices}
        if span.intersection({'nao', 'nunca', 'jamais', 'sem'}) - chosen:
            raise ValueError('Título omite negação da expressão de origem.')
        signature = tuple(ids)
        if signature not in seen:
            titles.append({**title, 'text': ' '.join(words[i]['word'] for i in indices)})
            seen.add(signature)
    visuals, visual_review = [], []
    spoken = ' '.join(normalized(w['word']) for w in words)
    for visual in result['visuais']:
        if visual.get('ancora') not in lookup:
            raise ValueError('Sugestão visual com ID inexistente.')
        if lookup[visual['ancora']][0] in discarded:
            continue
        description = visual.get('descricao', '')
        if not isinstance(description, str) or not 1 <= len(description) <= 180:
            raise ValueError('Descrição visual inválida.')
        # Exclui pictogramas Unicode: os ícones serão assets escolhidos pelo editor.
        if any(ord(c) >= 0x1F000 for c in description):
            raise ValueError('Sugestão visual com emoji; análise recusada.')
        if any(term in normalized(description) and term not in spoken
               for term in ('desmame', 'dosagem', 'cura', 'garantia')):
            visual_review.append({**visual, 'motivo': 'Conceito clínico/promessa não explicitado na fala.'})
            continue
        visuals.append(visual)
    return {'repeticoes': accepted, 'revisar_repeticoes': review, 'titulos': titles,
            'visuais': visuals, 'revisar_visuais': visual_review}


def analyze(metadata, transcripts):
    model = selected_model()
    words = word_catalog(transcripts)
    request_schema = schema_for(words)
    briefing = (ROOT.parents[1] / 'prompts' / 'BRIEFING_EDITORIAL.md').read_text(encoding='utf-8')
    payload = [{'id': w['id'], 'palavra': w['word']} for w in words]
    fingerprint = hashlib.sha256(json.dumps({'words': words, 'briefing': briefing, 'schema': request_schema,
                                             'model': model, 'fallback_model': client_options().get('fallback_model')}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = metadata / f'analise-editorial-{fingerprint[:16]}.json'
    if path.exists():
        cached = json.loads(path.read_text(encoding='utf-8'))
        if cached.get('fingerprint') == fingerprint:
            adjusted, changes = preserve_negations(cached['response'], words)
            try:
                checked = validate_response(adjusted, words)
            except (ValueError, TypeError, KeyError):
                if cached.get('status') != 'recusado':
                    raise
            else:
                if cached.get('status') == 'recusado' or changes:
                    atomic_json(path, {**cached, 'status': 'validado', 'validation_error': None,
                                       'local_adjustments': changes, 'validated': checked})
                return checked, words, path, True
    raw, usage = generate(briefing, json.dumps({'transcricao': payload}, ensure_ascii=False), schema=request_schema, model=model)
    record = {'fingerprint': fingerprint, 'created_at': datetime.now().astimezone().isoformat(),
                       'model': usage.get('model_used', model), 'requested_model': model,
                       'usage': usage, 'response': raw}
    # Salvar também a resposta rejeitada, sem usá-la como cache válido.
    try:
        adjusted, changes = preserve_negations(raw, words)
        checked = validate_response(adjusted, words)
    except (ValueError, TypeError, KeyError) as error:
        atomic_json(path, {**record, 'status': 'recusado', 'validation_error': str(error)})
        raise ValueError(f'{error} Resposta salva em {path.name}; nenhum título aplicado.') from None
    atomic_json(path, {**record, 'status': 'validado', 'local_adjustments': changes, 'validated': checked})
    return checked, words, path, False


def apply_repetitions(sources, words, analysis):
    """Combina cortes semânticos com os silêncios, mantendo fonte recuperável."""
    plans = []
    lookup = {w['id']: w for w in words}
    semantic = [[] for _ in sources]
    for repetition in analysis['repeticoes']:
        start = lookup[repetition['inicio_descartar']]
        keep = lookup[repetition['inicio_manter']]
        for index in range(start['source'], keep['source']+1):
            a = start['start'] if index == start['source'] else 0
            b = keep['start'] if index == keep['source'] else sources[index]['duration']
            # Pequeno respiro antes da tomada válida, sem apagar seu início.
            if index == keep['source']:
                b = max(a, b - .02)
            if b > a:
                semantic[index].append({'start': a, 'end': b})
    for index, source in enumerate(sources):
        fps = float(Fraction(source['fps']))
        cuts = [{'start': a / fps, 'end': b / fps} for a, b in source['removed_frames']]
        cuts.extend(semantic[index])
        # União em segundos antes da quantização conservadora.
        merged = []
        for cut in sorted(cuts, key=lambda c: c['start']):
            if merged and cut['start'] <= merged[-1]['end']:
                merged[-1]['end'] = max(merged[-1]['end'], cut['end'])
            else:
                merged.append(dict(cut))
        if len(merged) == 1 and merged[0]['start'] <= 0 and merged[0]['end'] >= source['duration']:
            # Uma tentativa inteiramente descartada pode ocupar um arquivo inteiro.
            removed, keeps = [(0, source['frames'])], []
        else:
            removed, keeps = quantize(merged, source['frames'], fps)
        plans.append({**source, 'removed_frames': removed, 'kept_frames': keeps,
                      'semantic_cuts': semantic[index]})
    if not any(plan['kept_frames'] for plan in plans):
        raise ValueError('A análise eliminaria a edição inteira; revisar as retomadas.')
    return plans


def map_anchor(word, sources):
    offset = 0.0
    for index, source in enumerate(sources):
        fps = float(Fraction(source['fps']))
        for start, end in source['kept_frames']:
            a, b = start / fps, end / fps
            if index == word['source'] and a <= word['start'] < b:
                return offset + word['start'] - a
            if index == word['source'] and word['start'] <= a < word['end']:
                return offset
            offset += b-a
    return None


def title_timeline(analysis, words, sources):
    lookup = {w['id']: w for w in words}
    titles = []
    for title in analysis['titulos']:
        start = map_anchor(lookup[title['palavras'][0]], sources)
        if start is not None:
            titles.append({**title, 'start': start, 'end': start + 2.5})
    titles.sort(key=lambda t: t['start'])
    duration = sum((b-a) / float(Fraction(s['fps'])) for s in sources for a, b in s['kept_frames'])
    for index, title in enumerate(titles):
        title['end'] = min(title['end'], duration,
                           titles[index+1]['start'] if index+1 < len(titles) else duration)
    return [t for t in titles if t['end'] > t['start']]


def prepare(edition):
    """Prepara plano verificável. Não simula montagem nativa ainda indisponível."""
    import contextlib
    import io
    from exportar_premiere import report
    from legendar_premiere import get_transcript, settings
    from progresso_etapas import StageProgress
    metadata = edition / 'metadados'
    with StageProgress() as progress:
        progress.stage('Localizando vídeo e transcrição')
        state = json.loads((metadata / 'estado-premiere.json').read_text(encoding='utf-8'))
        plan_path = edition / state['plan']
        if file_hash(plan_path) != state['plan_sha256']:
            raise ValueError('Plano de cortes alterado externamente.')
        sources = json.loads(plan_path.read_text(encoding='utf-8'))['sources']
        transcripts = []
        with contextlib.redirect_stdout(io.StringIO()):
            for source in sources:
                if file_hash(Path(source['path'])) != source['sha256']:
                    raise ValueError('Original mudou desde os cortes.')
                transcript, _ = get_transcript(source, metadata, settings())
                transcripts.append(transcript)
        progress.done()
        progress.stage('Conferindo os cortes existentes')
        for source in sources:
            if not source['kept_frames']:
                raise ValueError('Plano sem trechos mantidos.')
        progress.done()
        progress.stage('Conferindo a preparação de áudio')
        for source in sources:
            audio = source.get('treated_audio')
            if audio and file_hash(Path(audio['path'])) != audio['sha256']:
                raise ValueError('Áudio do plano mudou externamente.')
        progress.done()
        progress.stage('Analisando o texto e selecionando títulos')
        analysis, words, cache, reused = analyze(metadata, transcripts)
        progress.done()
        progress.stage('Organizando sugestões para a pré-edição')
        plans = apply_repetitions(sources, words, analysis)
        titles = title_timeline(analysis, words, plans)
        visuals = []
        lookup = {w['id']: w for w in words}
        for visual in analysis['visuais']:
            start = map_anchor(lookup[visual['ancora']], plans)
            if start is not None:
                visuals.append({**visual, 'start': start})
        output = metadata / 'plano-editorial-pendente.json'
        atomic_json(output, {'model': MODEL, 'analysis_cache': cache.name, 'cache_reused': reused,
                             'base_plan_sha256': state['plan_sha256'], 'sources': plans,
                             'titles': titles, 'visuals': visuals, 'analysis': analysis,
                             'status': 'PREPARADO; não aplicado ao projeto nativo',
                             'pending': 'Inserção gráfica editável aguarda modelo-titulos.prproj e integração nativa.'})
        lines = [f"- {t['start']:.3f}s → {t['end']:.3f}s: **{t['text']}** ({t['categoria']})" for t in titles]
        lines += [f"- {v['start']:.3f}s: ({v['descricao']})" for v in visuals]
        lines += [f"- Revisar sugestão não aplicada: {v['descricao']} — {v['motivo']}" for v in analysis['revisar_visuais']]
        usage = json.loads(cache.read_text(encoding='utf-8')).get('usage', {})
        report(metadata,
               f'- Status: ANÁLISE PREPARADA; títulos/repetições ainda NÃO aplicados ao .prproj.\n'
               f'- Modelo: {MODEL}; cache {"reutilizado" if reused else "novo"}. Nenhuma chave nos dados enviados.\n'
               f'- Consumo informado pela API na geração original: entrada {usage.get("promptTokenCount", "n/d")}; saída {usage.get("candidatesTokenCount", "n/d")}; total {usage.get("totalTokenCount", "n/d")}. Reutilizar cache não faz nova chamada. Cota/gratuidade dependem da conta Google, não são garantidas pelo programa.\n'
               f'- Títulos: {len(titles)}; sugestões visuais: {len(visuals)}; grupos de repetição validados: {len(analysis["repeticoes"])}; ambíguos: {len(analysis["revisar_repeticoes"])}; visuais para revisão: {len(analysis["revisar_visuais"])}.\n'
               '- Tempos calculados pelo Python; último take prevalece em repetições validadas. Original preservado.\n'
               '- Para título simples editável, fornecer modelo com gráfico de texto nativo. Não foi inventado um gráfico nem alterado o projeto aprovado.\n\n' + '\n'.join(lines),
               title='Análise Gemini', section='## Direção editorial')
        progress.label = 'Plano editorial salvo — montagem gráfica pendente do modelo'
        progress.done()
    return output


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--edition', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(prepare(args.edition.resolve()))
    except Exception as error:
        print(f'ERRO: {error}')
        raise SystemExit(1)
