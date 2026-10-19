param([switch]$Verificar)
$ErrorActionPreference = 'Stop'
$raizPreEdit = Split-Path $PSScriptRoot -Parent
$pythonPreEdit = Join-Path $PSScriptRoot 'runtime\python\python.exe'
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$env:PREEDITOR_CLIENTES = Join-Path $raizPreEdit 'CLIENTES'
$env:PATH = (Join-Path $PSScriptRoot 'runtime\ffmpeg\bin') + ';' + $env:PATH
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
$env:HF_HUB_DISABLE_PROGRESS_BARS = '0'
$env:HF_HUB_DISABLE_IMPLICIT_TOKEN = '1'
$env:HF_HUB_ETAG_TIMEOUT = '30'
$env:HF_HUB_DOWNLOAD_TIMEOUT = '120'
$travaPreEdit = $null
$retornoPreEdit = 1
function Get-PreEditFileHash([string]$CaminhoPreEdit) {
    $leituraPreEdit = [IO.File]::OpenRead($CaminhoPreEdit)
    $algoritmoPreEdit = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algoritmoPreEdit.ComputeHash($leituraPreEdit)).Replace('-', '').ToLowerInvariant() }
    finally { $algoritmoPreEdit.Dispose(); $leituraPreEdit.Dispose() }
}
try {
    if ($env:OS -ne 'Windows_NT') { throw 'Este instalador e exclusivo para Windows. Nao usar no Mac.' }
    $buildPreEdit = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').CurrentBuildNumber
    if ([int]$buildPreEdit -lt 10240) { throw 'Use Windows 10/11 x64 atualizado.' }
    $arquiteturaPreEdit = $env:PROCESSOR_ARCHITEW6432
    if (-not $arquiteturaPreEdit) { $arquiteturaPreEdit = $env:PROCESSOR_ARCHITECTURE }
    if ($arquiteturaPreEdit -ne 'AMD64') { throw 'Este pacote exige Windows x64. Windows ARM/32 bits precisa de outro pacote.' }
    if ($raizPreEdit.StartsWith('\\')) { throw 'Extraia para uma pasta local, nao uma pasta de rede.' }
    foreach ($protegidaPreEdit in @($env:windir, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($protegidaPreEdit -and ($raizPreEdit + '\').StartsWith($protegidaPreEdit.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Extraia para uma pasta sua fora de Windows/Program Files. O editor precisa poder gravar nela sem administrador.'
        }
    }
    New-Item -ItemType Directory -Force -Path $env:PREEDITOR_CLIENTES | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'relatorios') | Out-Null
    try {
        $travaPreEdit = [IO.File]::Open((Join-Path $PSScriptRoot 'instalacao.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch { throw 'Outra instalacao esta aberta, ou a pasta nao permite gravacao. Feche a outra janela antes de repetir.' }
    if ($raizPreEdit.Length -gt 120) { Write-Host 'AVISO: caminho longo. Prefira uma pasta curta para evitar limites do Windows.' -ForegroundColor Yellow }
    $volumePreEdit = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($raizPreEdit))
    Write-Host 'PRE EDIT - instalacao por Daniel Fileto' -ForegroundColor Cyan
    Write-Host '[1/4] Conferindo Python, FFmpeg e arquivos do pacote...' -ForegroundColor Yellow
    if (-not (Test-Path -LiteralPath $pythonPreEdit)) { throw 'Extraia o ZIP inteiro antes de executar.' }
    $manifestoPreEdit = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'manifesto.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $hashPythonPreEdit = $manifestoPreEdit.sha256.'runtime/python/python.exe'
    if (-not $hashPythonPreEdit -or (Get-PreEditFileHash $pythonPreEdit) -ne $hashPythonPreEdit) { throw 'Python incompleto/alterado. Extraia novamente o pacote original.' }
    & $pythonPreEdit -s -u (Join-Path $PSScriptRoot 'preparar.py') --integridade
    if ($LASTEXITCODE -ne 0) { throw 'Falha na integridade do pacote.' }
    & $pythonPreEdit -s -u (Join-Path $PSScriptRoot 'preparar.py') --modelo-pronto
    if (-not $Verificar -and $LASTEXITCODE -ne 0 -and $volumePreEdit.AvailableFreeSpace -lt 5GB) { throw 'Reserve pelo menos 5 GB livres para preparacao e cache do modelo.' }
    Write-Host '[2/4] Conferindo bibliotecas locais...' -ForegroundColor Yellow
    & $pythonPreEdit -s -u (Join-Path $PSScriptRoot 'preparar.py') --bibliotecas
    $codigoBibliotecasPreEdit = $LASTEXITCODE
    if ($codigoBibliotecasPreEdit -ne 0) {
        if ($codigoBibliotecasPreEdit -ne 20 -or $Verificar) { throw 'Bibliotecas indisponiveis/incompativeis. Consulte a mensagem acima; nao sera instalado VC++ indiscriminadamente.' }
        Write-Host 'Preparando Microsoft Visual C++ Runtime. O Windows pode pedir autorizacao.' -ForegroundColor Yellow
        Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
        $vcPreEdit = Join-Path $PSScriptRoot 'runtime\vc_redist.x64.exe'
        $downloadPreEdit = $vcPreEdit + '.download.exe'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        for ($tentativaPreEdit = 1; $tentativaPreEdit -le 3; $tentativaPreEdit++) {
            try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 180 -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $downloadPreEdit; break }
            catch { if ($tentativaPreEdit -eq 3) { throw }; Write-Host 'Download indisponivel; tentando novamente...' -ForegroundColor Yellow; Start-Sleep -Seconds 3 }
        }
        $assinaturaPreEdit = Get-AuthenticodeSignature -LiteralPath $downloadPreEdit
        if ($assinaturaPreEdit.Status -ne 'Valid' -or $assinaturaPreEdit.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { throw 'Assinatura Microsoft nao confirmada; instalacao cancelada.' }
        Move-Item -LiteralPath $downloadPreEdit -Destination $vcPreEdit -Force
        $processoPreEdit = Start-Process -FilePath $vcPreEdit -ArgumentList '/install','/quiet','/norestart' -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        if ($processoPreEdit.ExitCode -notin @(0,3010)) { throw 'Falha/cancelamento ao instalar Visual C++ Runtime.' }
        if ($processoPreEdit.ExitCode -eq 3010) { Write-Host 'Visual C++ recomenda reiniciar o Windows apos a instalacao.' -ForegroundColor Yellow }
        & $pythonPreEdit -s -u (Join-Path $PSScriptRoot 'preparar.py') --bibliotecas
        if ($LASTEXITCODE -ne 0) { throw 'Bibliotecas continuam indisponiveis. Se o Windows pediu reinicio, reinicie antes de repetir.' }
    }
    & $pythonPreEdit -s -u (Join-Path $PSScriptRoot 'preparar.py') --capacidades
    if ($LASTEXITCODE -ne 0) { throw 'Falha no diagnostico de GPU/FFmpeg.' }
    $capacidadesPreEdit = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'capacidades.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $argumentosConfiguracaoPreEdit = @('--configurar')
    if ($Verificar) { $argumentosConfiguracaoPreEdit += '--somente-verificar' }
    elseif ($capacidadesPreEdit.editor -eq 'premiere' -and -not $capacidadesPreEdit.hdr_sdr) {
        Write-Host 'ATENCAO: conversao HDR indisponivel. Atualize o driver da GPU para manter a cor aprovada.' -ForegroundColor Yellow
        $respostaPreEdit = Read-Host 'Continuar a preparacao apenas para videos SDR? [s/N]'
        if ($respostaPreEdit -notmatch '^(?i:s|sim|yes|y)$') { throw 'Preparacao cancelada ate corrigir o suporte HDR. Nenhum driver foi alterado.' }
        $argumentosConfiguracaoPreEdit += '--permitir-sdr'
    }
    Write-Host '[3/4] Preparando modelo de transcricao...' -ForegroundColor Yellow
    if ($Verificar) { $modoPreEdit = '--sem-download' } else { $modoPreEdit = '--modelo' }
    & $pythonPreEdit -s -u (Join-Path $PSScriptRoot 'preparar.py') $modoPreEdit
    if ($LASTEXITCODE -ne 0) { throw 'Falha/interrupcao ao preparar o modelo. Internet pode ser necessaria. Rode novamente para retomar o cache.' }
    Write-Host '[4/4] Conferindo configuracao e modelos de projeto...' -ForegroundColor Yellow
    & $pythonPreEdit -s -u (Join-Path $PSScriptRoot 'preparar.py') @argumentosConfiguracaoPreEdit
    if ($LASTEXITCODE -ne 0) { throw 'Falha na configuracao do pre-editor.' }
    if ($Verificar) { Write-Host 'Verificacao sem download concluida. Nao substitui a instalacao completa.' -ForegroundColor Green }
    else { Write-Host 'Pronto. Use Pre Editor.bat. CLIENTES\Cliente\Edicao recebe os videos.' -ForegroundColor Green }
    $retornoPreEdit = 0
} catch {
    $mensagemPreEdit = $_.Exception.Message
    Write-Host "ERRO: $mensagemPreEdit" -ForegroundColor Red
    try { Add-Content -LiteralPath (Join-Path $PSScriptRoot 'relatorios\instalacao-erros.log') -Value $mensagemPreEdit -Encoding UTF8 } catch {}
} finally {
    if ($travaPreEdit) { $travaPreEdit.Dispose() }
    if (-not $Verificar) { Read-Host 'Pressione ENTER para sair' | Out-Null }
}
exit $retornoPreEdit
