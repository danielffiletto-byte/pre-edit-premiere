param([switch]$Verificar)
$ErrorActionPreference = 'Stop'
$raizPreEdit = Split-Path $PSScriptRoot -Parent
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$env:PIPELINE_COLOR = '1'
$env:PREEDITOR_CLIENTES = Join-Path $raizPreEdit 'CLIENTES'
$env:PATH = (Join-Path $PSScriptRoot 'runtime\ffmpeg\bin') + ';' + $env:PATH
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$pythonPreEdit = Join-Path $PSScriptRoot 'runtime\python\python.exe'
$resultadoPreEdit = 1
try {
    & $pythonPreEdit -s -u (Join-Path $PSScriptRoot 'preparar.py') --pronto
    if ($LASTEXITCODE -ne 0) {
        $argumentosInstalacaoPreEdit = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'Instalar.ps1'))
        if ($Verificar) { $argumentosInstalacaoPreEdit += '-Verificar' }
        & powershell.exe @argumentosInstalacaoPreEdit
        if ($LASTEXITCODE -ne 0) { throw 'Execute Instalar.bat para concluir a preparacao.' }
    }
    & $pythonPreEdit -s -u (Join-Path $PSScriptRoot 'preparar.py') --relocalizar
    if ($LASTEXITCODE -ne 0) { throw 'Nao foi possivel ajustar os caminhos do pacote.' }
    $argumentosPreEdit = @('-s', '-u', (Join-Path $PSScriptRoot 'ferramentas\premiere\supervisionar.py'))
    if ($Verificar) { $argumentosPreEdit += '--verificar' }
    & $pythonPreEdit @argumentosPreEdit
    $resultadoPreEdit = $LASTEXITCODE
} catch {
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    if (-not $Verificar) { Read-Host 'Pressione ENTER para sair' | Out-Null }
}
exit $resultadoPreEdit
