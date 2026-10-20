# Primeiro computador — Premiere/Windows

1. Copie somente **Pre Editor Premiere.bat** para uma pasta local sua, fora de Windows/Program Files. Use o usuário Windows do editor; não execute como outro usuário.
2. Clique nele: cria `Edição/Clientes` e `Edição/Metadados` e busca a versão atual no GitHub público. **Não pede login, token GitHub nem cadastro/autorização por hash.**
3. A única chave solicitada é a **Gemini**, para análise de texto/títulos. Fica criptografada pelo Windows (DPAPI), vinculada a este computador/usuário, nunca no Git/código. É reutilizada automaticamente. Cole somente a chave inteira usando clique direito ou Shift+Insert.
4. O ambiente (~300 MB) e o modelo de transcrição (~1,5 GB) são baixados uma vez. Reserve ao menos 5 GB livres. Se necessário, o Windows pode pedir confirmação para instalar Visual C++ oficial. HDR exige driver/Vulkan compatível; não é aplicado outro look em silêncio.
5. Organize vídeos em `Edição/Clientes/Cliente/Tema` e use o mesmo BAT. Projetos e dados de trabalho ficam em `Edição/Metadados/Trabalhos/Cliente/Tema/premiere`. O Premiere deve estar instalado separadamente.

## Acesso e atualização

- Toda execução consulta o GitHub público anonimamente; sem internet/download válido, não roda nem usa motor antigo como fallback. Limites temporários do GitHub são informados; não há pedido de token para contorná-los.
- Código, prompt e parâmetros editoriais são baixados para `Metadados/Temporarios`, conferidos por hash, executados e removidos ao encerrar normalmente. Fechar a janela à força/desligar o computador pode deixar temporários: não é garantia de apagamento forense.
- Python, FFmpeg, bibliotecas genéricas, Whisper, recursos usados nos projetos, áudios e caches ficam em Metadados para não baixar/refazer tudo. Não apagar Referencias: os projetos podem apontar para elas.
- Projetos permanecem não destrutivos: vídeo original é referenciado em Clientes. Mover/apagar originais ou a pasta Edição pode exigir relink no Premiere.
- Código e heurísticas agora são públicos e podem ser copiados. Não há bloqueio individual por computador. Tornar privado no futuro impede novos downloads anônimos, mas não recolhe cópias já obtidas.
- `Pre Editor Premiere.bat --configurar`: informar novamente a chave Gemini. `--verificar`: verificar ambiente/pastas sem editar nem consumir API. `--cadastro` apenas informa que não há cadastro e sai.
- Não enviar senhas ou chaves pelo chat. A chave Gemini é a única credencial local; espaços/quebras nas bordas são limpos e controles internos recusados antes de salvar.
- Para migrar: substituir apenas o BAT antigo por esta versão, na MESMA pasta. Não apagar Edição/Metadados nem reinstalar o ambiente. Token/hash antigos não são lidos nem exigidos; arquivos antigos de acesso não são apagados automaticamente.

## Administração

Atualizações editoriais: editar fontes/config/prompt e publicar em `main`. O lançador usa os hashes da árvore Git daquele commit, gerados automaticamente pelo GitHub; não é preciso trocar hashes manualmente. Runtime é um asset público versionado e imutável da release `windows-x64-v1`; atualizações de código não baixam o runtime de novo. Alterações do próprio lançador podem exigir trocar o BAT.

Primeira entrega testada em ambiente Windows existente, com mídia sintética e rede simulada. Instalação em Windows limpo e revisão visual no Premiere do editor exigem o teste no computador de destino. CapCut Mac é uma entrega separada, ainda pendente.
