# Pre Edit Premiere

Pré-edição de vídeo por Daniel Fileto. Repositório privado de código-fonte.

## Estado

- Windows x64; gerar projeto Premiere nativo com camadas editáveis.
- Transcrição local, cortes de pausas/retomadas, tratamento de áudio, legendas e títulos via Gemini.
- Pico-alvo 0 dBFS, com aumento total máximo de +6 dB por trecho.
- Legendas de até cinco palavras/duas linhas, separadas também pela pontuação.
- Originais preservados; pré-edição e textos continuam editáveis no programa.
- Conversão HDR→Rec.709 preserva a precisão temporal do original, sem arredondar os quadros para FPS nominal. Confere os instantes individuais; em falha temporal do encoder GPU, tenta CPU uma vez com a mesma cor e precisão. Se continuar inválido, interrompe sem publicar a cópia. SDR válido já existente continua sendo reaproveitado.

## Instalar pelo lançador online — Windows

Use **Pre Editor Online.bat** (ou o arquivo de entrega **Pre Editor Premiere.bat**) numa pasta local sua. O primeiro uso cria `Edição/Clientes` e `Edição/Metadados`, gera o hash do computador e aguarda aprovação manual em [`acesso/computadores.json`](acesso/computadores.json). Nada se autoriza sozinho.

Após aprovação, configure na janela local um token GitHub somente-leitura deste repositório e a chave Gemini. Ambos ficam protegidos pelo Windows/DPAPI, fora do Git. O BAT confere autorização e versão na internet em cada execução; sem acesso ou sem autorização não usa código antigo. Instruções: [`online/LEIA-ME.md`](online/LEIA-ME.md).

O token salvo é usado automaticamente; o lançador só pede substituição após rejeição de autenticação pelo GitHub. Colagem com controles invisíveis é recusada antes do HTTP; espaços/quebras nas bordas são limpos. Para receber esta correção do próprio lançador, substitua apenas o BAT na mesma pasta, preservando Edição e o ambiente já instalado.

Código, prompt e configurações do motor são executados temporariamente e removidos no encerramento normal. Runtime e modelo de transcrição são baixados uma vez e guardados em Metadados; projetos, áudios tratados, recursos de referência e caches também ficam lá. Os vídeos originais permanecem em Clientes. Fechamento forçado/desligamento pode deixar temporários; isso não é proteção impossível de burlar.

## Ambiente e credenciais

Este Git contém fontes, configurações e modelos técnicos. Não contém chaves ou materiais de clientes. O runtime privado está na release `windows-x64-v1`, baixada e validada pelo lançador; o modelo Whisper vem do cache/download com revisão fixada. Os antigos `Instalar.bat` e `Pre Editor.bat` são do pacote offline, não a entrada recomendada para a instalação online.

Configure `GEMINI_API_KEY` no ambiente ou em `metadados/config/.env` local. O exemplo está vazio. Nunca comite chaves, tokens ou arquivos de clientes, mesmo neste repositório privado.

## Pastas

- `Edição/Clientes/Cliente/Edicao/`: vídeos organizados pelo editor, fora do Git.
- `Edição/Metadados/Trabalhos/Cliente/Edicao/premiere/`: projetos e áudios tratados.
- `metadados/ferramentas/`: motor Python e utilitários.
- `metadados/prompts/BRIEFING_EDITORIAL.md`: regras editoriais.
- `metadados/config/`: parâmetros e preset SDR aprovado.
- `metadados/prproj/`: modelo técnico, não um projeto de cliente.

## Distribuição pela internet

O novo lançador online baixa a versão atual por commit e valida hashes antes de executar. Isso não altera BATs offline antigos já enviados. Autenticação do GitHub no chat não autentica o computador dos editores. Cada instalação precisa de acesso próprio, revogável; hash de cadastro é estável enquanto Windows/usuário não mudarem.

Código executado localmente pode ser copiado. A limpeza não é proteção contra cópia técnica. Recursos referenciados pelos projetos e caches de processamento precisam continuar disponíveis depois da execução.
