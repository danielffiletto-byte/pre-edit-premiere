# Pre Edit Premiere

Pré-edição de vídeo por Daniel Fileto. Repositório privado de código-fonte.

## Estado

- Windows x64; gerar projeto Premiere nativo com camadas editáveis.
- Transcrição local, cortes de pausas/retomadas, tratamento de áudio, legendas e títulos via Gemini.
- Pico-alvo 0 dBFS, com aumento total máximo de +6 dB por trecho.
- Legendas de até cinco palavras/duas linhas, separadas também pela pontuação.
- Originais preservados; pré-edição e textos continuam editáveis no programa.

## Ambiente e credenciais

Este Git contém fontes, configurações e modelos técnicos. Não contém Python/FFmpeg privados, bibliotecas, modelo Whisper, chaves ou materiais de clientes. Clonar/baixar este repositório sozinho NÃO substitui a preparação do ambiente do editor. O instalador atual depende do runtime privado fornecido separadamente; baixar esse runtime automaticamente é uma etapa futura.

Configure `GEMINI_API_KEY` no ambiente ou em `metadados/config/.env` local. O exemplo está vazio. Nunca comite chaves, tokens ou arquivos de clientes, mesmo neste repositório privado.

## Pastas

- `CLIENTES/Cliente/Edicao/`: vídeos organizados pelo editor, fora do Git.
- `metadados/ferramentas/`: motor Python e utilitários.
- `metadados/prompts/BRIEFING_EDITORIAL.md`: regras editoriais.
- `metadados/config/`: parâmetros e preset SDR aprovado.
- `metadados/prproj/`: modelo técnico, não um projeto de cliente.

## Distribuição pela internet

O lançador que baixa o código atualizado para uma pasta temporária, executa e limpa ainda será integrado. Esta publicação NÃO ativa atualização automática nos BATs existentes. Autenticação do GitHub no chat não autentica o computador dos editores. Cada instalação precisará de acesso próprio, revogável.

Código executado localmente pode ser copiado. A limpeza não é proteção contra cópia técnica. Recursos referenciados pelos projetos e caches de processamento precisam continuar disponíveis depois da execução.
