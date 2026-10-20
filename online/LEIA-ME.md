# Primeiro computador — Premiere/Windows

1. Copie somente **Pre Editor Premiere.bat** para uma pasta local sua, fora de Windows/Program Files. Use o usuário Windows do editor; não execute como outro usuário.
2. Clique nele: cria `Edição/Clientes` e `Edição/Metadados`. O primeiro uso gera o hash em `Edição/Metadados/Acesso/COMPUTADOR.md` e para.
3. Envie o hash ao Daniel. Ele inclui uma entrada habilitada em `acesso/computadores.json` no GitHub privado. A instalação não se autoriza sozinha.
4. Execute novamente. Será pedido um token GitHub **fine-grained, Contents: Read-only**, somente para `pre-edit-premiere`, e depois a chave Gemini. Eles ficam criptografados pelo Windows (DPAPI), vinculados a este computador/usuário, nunca no Git/código. Daniel pode fazer essa configuração pessoalmente. Use token separado por editor, revogável, com expiração; não use o token de proprietário com permissões de escrita.
5. O ambiente (~300 MB) e o modelo de transcrição (~1,5 GB) são baixados uma vez. Reserve ao menos 5 GB livres. Se necessário, o Windows pode pedir confirmação para instalar Visual C++ oficial. HDR exige driver/Vulkan compatível; não é aplicado outro look em silêncio.
6. Organize vídeos em `Edição/Clientes/Cliente/Tema` e use o mesmo BAT. Projetos e dados de trabalho ficam em `Edição/Metadados/Trabalhos/Cliente/Tema/premiere`. O Premiere deve estar instalado separadamente.

## Acesso e atualização

- Toda execução consulta o GitHub; sem internet, sem acesso ou sem hash habilitado, não roda. Não usa motor antigo como fallback.
- Código, prompt e parâmetros editoriais são baixados para `Metadados/Temporarios`, conferidos por hash, executados e removidos ao encerrar normalmente. Fechar a janela à força/desligar o computador pode deixar temporários: não é garantia de apagamento forense.
- Python, FFmpeg, bibliotecas genéricas, Whisper, recursos usados nos projetos, áudios e caches ficam em Metadados para não baixar/refazer tudo. Não apagar Referencias: os projetos podem apontar para elas.
- Projetos permanecem não destrutivos: vídeo original é referenciado em Clientes. Mover/apagar originais ou a pasta Edição pode exigir relink no Premiere.
- O hash de cadastro identifica Windows + usuário. Trocar usuário/reinstalar Windows pode exigir nova autorização. Remova/desabilite o hash para bloquear próximas execuções. Isso não impede cópia técnica do código ou dos projetos já entregues.
- `Pre Editor Premiere.bat --cadastro`: mostrar hash. `--configurar`: digitar credenciais novamente. `--verificar`: verificar ambiente e pastas sem editar nem consumir API.
- O GitHub conectado ao chat não autentica este BAT. Não enviar senha, token ou chave pelo chat; inserir somente na janela local.

## Administração

Autorização: `acesso/computadores.json`, formato `{"schema":1,"devices":[{"hash":"HASH_DE_64_CARACTERES","name":"Editor 1","enabled":true}]}`. A lista começa vazia. Só o dono edita essa lista; token de editor não tem escrita.

Atualizações editoriais: editar fontes/config/prompt e publicar em `main`. O lançador usa os hashes da árvore Git daquele commit, gerados automaticamente pelo GitHub; não é preciso trocar hashes manualmente. Hash do computador não muda. Runtime é um asset versionado e imutável da release `windows-x64-v1`; atualizações de código não baixam o runtime de novo.

Primeira entrega testada em ambiente Windows existente, com mídia sintética e rede simulada. Instalação em Windows limpo e revisão visual no Premiere do editor exigem o teste no computador de destino. CapCut Mac é uma entrega separada, ainda pendente.
