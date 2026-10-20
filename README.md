# Pre Edit Premiere

Pré-edição de vídeo por Daniel Fileto — Windows x64, Premiere nativo editável.

## Instalação e uso

Copie **Pre Editor Online.bat** para uma pasta local sua (arquivo de entrega: **Pre Editor Premiere.bat**) e execute. Cria `Edição/Clientes` e `Edição/Metadados`. **Sem login, token GitHub ou autorização por hash.**

A única chave necessária é Gemini, para análise de texto/títulos. É informada na janela local, guardada com Windows DPAPI e reutilizada automaticamente. Não publique chaves ou materiais de clientes. Instruções completas: [online/LEIA-ME.md](online/LEIA-ME.md).

Python, FFmpeg, bibliotecas e Whisper ficam no cache de Metadados. Projetos, áudios, recursos de referência, relatórios e dados de trabalho também ficam lá. Instale o Premiere separadamente.

## Pré-edição

- Transcrição local, cortes não destrutivos de pausas/retomadas, áudio tratado, legendas e títulos via Gemini.
- Pico-alvo 0 dBFS; aumento positivo total máximo +6 dB por trecho.
- Legendas de até cinco palavras e duas linhas, separadas também pela pontuação.
- Originais preservados; textos, ajuste de cor e faixas de áudio continuam separados/editáveis.
- HDR HLG compatível → Rec.709 com o preset spline aprovado, precisão temporal original e conferência dos instantes dos quadros. Em falha temporal GPU, uma tentativa CPU com mesmo preset; falha persistente interrompe sem publicar cópia insegura. SDR válido existente é reaproveitado.

## Pastas

- `Edição/Clientes/Cliente/Tema/`: vídeos do editor, fora do Git.
- `Edição/Metadados/Trabalhos/Cliente/Tema/premiere/`: projetos e áudios tratados.
- `metadados/ferramentas/`: motor.
- `metadados/config/` e `metadados/prompts/`: parâmetros e briefing.
- `metadados/prproj/`: modelo técnico, não projeto de cliente.

## Atualizações e limites

O BAT busca main sem autenticação, valida o commit/arquivos e executa o motor temporariamente. Sem internet/download válido, não usa motor antigo como fallback. Motor/config/prompt novos chegam na próxima execução; alterações do lançador podem exigir trocar somente o BAT.

Para migrar da versão autenticada, substitua o BAT na mesma pasta, sem apagar Edição. Credenciais GitHub/hash antigos são ignorados. Os antigos `Instalar.bat` e `Pre Editor.bat` do pacote offline não são a entrada recomendada.

Runtime público em `windows-x64-v1`, conferido por SHA256; fonte conferida pelos hashes da árvore Git. A limpeza remove somente a sessão própria no encerramento normal. Fechamento forçado pode deixar temporários. Não mover/apagar originais ou referências usadas pelos projetos.

O código/heurísticas são públicos e podem ser copiados; não há bloqueio individual por computador. Nenhuma chave ou mídia de cliente faz parte desta distribuição. Revisão visual no Premiere e instalação em computador novo continuam sujeitas ao teste do editor. CapCut/Mac é uma entrega separada.
