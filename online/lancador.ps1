# Bootstrap operacional. Heurísticas e motor são baixados do repositório autorizado.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.Security
$script:Repo = 'danielffiletto-byte/pre-edit-premiere'
$script:Api = 'https://api.github.com/repos/' + $script:Repo

function Get-Digest([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose(); $stream.Dispose() }
}

function Get-TextDigest([string]$Value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Assert-NoLinks([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Pasta com link/junction: escolha uma pasta local normal.' }
        }
        $next = [IO.Path]::GetDirectoryName($current)
        if ($next -eq $current) { break }
        $current = $next
    }
}

function Get-SafeChild([string]$Base, [string]$Relative) {
    if (-not $Relative -or [IO.Path]::IsPathRooted($Relative) -or $Relative.Contains(':') -or ($Relative -split '[/\\]') -contains '..') { throw 'Caminho relativo invalido.' }
    $root = [IO.Path]::GetFullPath($Base).TrimEnd('\')
    $path = [IO.Path]::GetFullPath((Join-Path $root $Relative))
    if (-not $path.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Caminho fora da pasta autorizada.' }
    Assert-NoLinks $path
    return $path
}

function Write-JsonFile([string]$Path, $Value) {
    $temp = $Path + '.tmp'
    [IO.File]::WriteAllText($temp, ($Value | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temp -Destination $Path -Force
}

function Get-ComputerFingerprint {
    $registry = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Registry64)
    try { $key = $registry.OpenSubKey('SOFTWARE\Microsoft\Cryptography'); try { $machine = $key.GetValue('MachineGuid') } finally { if ($key) { $key.Dispose() } } } finally { $registry.Dispose() }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if (-not $machine -or -not $sid) { throw 'Nao foi possivel identificar este Windows/usuario.' }
    return Get-TextDigest ('preedit-computador-v1|'+$machine+'|'+$sid)
}

function Save-SecureSecret([string]$Path, [Security.SecureString]$Secure) {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    $bytes = $null
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer))
        $protected = [Security.Cryptography.ProtectedData]::Protect($bytes,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
        [IO.File]::WriteAllBytes($Path,$protected)
    } finally { if ($bytes) { [Array]::Clear($bytes,0,$bytes.Length) }; [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

function Read-Secret([string]$Folder, [string]$Name, [string]$Label, [bool]$Replace = $false) {
    $path = Get-SafeChild $Folder ($Name+'.protegido')
    if ($Replace -or -not (Test-Path -LiteralPath $path)) {
        $secure = Read-Host $Label -AsSecureString
        if ($secure.Length -eq 0) { throw 'Credencial nao informada.' }
        try { Save-SecureSecret $path $secure } finally { $secure.Dispose() }
    }
    $plain = $null
    try {
        $plain = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($path),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
        return [Text.Encoding]::UTF8.GetString($plain)
    } catch { throw 'Credencial pertence a outro Windows/usuario ou esta danificada. Execute com --configurar.' }
    finally { if ($plain) { [Array]::Clear($plain,0,$plain.Length) } }
}

function Assert-RemoteUri([Uri]$Uri, [bool]$Microsoft = $false) {
    if ($Uri.Scheme -ne 'https' -or $Uri.UserInfo -or $Uri.Port -ne 443) { throw 'Download fora de HTTPS bloqueado.' }
    $domains = if ($Microsoft) { @('aka.ms','download.visualstudio.microsoft.com','download.microsoft.com') } else { @('api.github.com','github.com','codeload.github.com','githubusercontent.com') }
    if (-not @($domains | Where-Object { $Uri.DnsSafeHost -eq $_ -or $Uri.DnsSafeHost.EndsWith('.'+$_,[StringComparison]::OrdinalIgnoreCase) }).Count) { throw 'Destino de download nao reconhecido.' }
}

function Get-RemoteFile([string]$Url, [string]$Output, [string]$Token, [long]$Limit = 20971520, [string]$Accept = 'application/vnd.github+json', [bool]$Microsoft = $false) {
    $uri = [Uri]$Url
    for ($redirect = 0; $redirect -lt 7; $redirect++) {
        Assert-RemoteUri $uri $Microsoft
        $request = [Net.HttpWebRequest]::Create($uri)
        $request.Method = 'GET'; $request.AllowAutoRedirect = $false
        $request.Timeout = 45000; $request.ReadWriteTimeout = 90000
        $request.UserAgent = 'PreEdit-DanielFileto/1.0'
        $request.Accept = $Accept
        # Segredo nunca acompanha redirecionamento/CDN.
        if ($redirect -eq 0 -and $uri.Host -eq 'api.github.com' -and $Token) {
            $request.Headers['Authorization'] = 'Bearer '+$Token
            $request.Headers['X-GitHub-Api-Version'] = '2022-11-28'
        }
        try { $response = $request.GetResponse() }
        catch [Net.WebException] {
            $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
            if ($_.Exception.Response) { $_.Exception.Response.Close() }
            throw "GitHub/download indisponivel (HTTP $status). Confira internet, token e acesso; nenhuma versao antiga sera executada."
        }
        try {
            if ([int]$response.StatusCode -in @(301,302,303,307,308)) {
                $uri = [Uri]::new($uri, $response.Headers['Location'])
                continue
            }
            if ($response.ContentLength -gt $Limit) { throw 'Download acima do tamanho permitido.' }
            $inputStream = $response.GetResponseStream()
            $outputStream = [IO.File]::Open($Output,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
            try {
                $buffer = New-Object byte[] 1048576
                $total = 0L; $last = [DateTime]::UtcNow
                while (($count = $inputStream.Read($buffer,0,$buffer.Length)) -gt 0) {
                    $total += $count
                    if ($total -gt $Limit) { throw 'Download excedeu o tamanho permitido.' }
                    $outputStream.Write($buffer,0,$count)
                    if (([DateTime]::UtcNow-$last).TotalSeconds -ge 4) {
                        $detail = if ($response.ContentLength -gt 0) { '{0:0}% | {1:0.0} MB' -f ($total*100.0/$response.ContentLength),($total/1MB) } else { '{0:0.0} MB recebidos' -f ($total/1MB) }
                        Write-Host ("`rBaixando: "+$detail+'           ') -NoNewline
                        $last = [DateTime]::UtcNow
                    }
                }
                if ($response.ContentLength -ge 0 -and $total -ne $response.ContentLength) { throw 'Download incompleto.' }
                if ($total -eq 0) { throw 'Download vazio.' }
            } finally { $outputStream.Dispose(); $inputStream.Dispose() }
            return
        } finally { $response.Close() }
    }
    throw 'Redirecionamentos demais; download recusado.'
}

function Get-RemoteJson([string]$Url, [string]$Token, [string]$Session) {
    $path = Join-Path $Session (([Guid]::NewGuid().ToString('N'))+'.json')
    Get-RemoteFile $Url $path $Token 2097152
    return [IO.File]::ReadAllText($path,[Text.Encoding]::UTF8) | ConvertFrom-Json
}

function Get-RepositoryJson([string]$Path, [string]$Ref, [string]$Token, [string]$Session) {
    $data = Get-RemoteJson ($script:Api+'/contents/'+$Path+'?ref='+$Ref) $Token $Session
    if ($data.encoding -ne 'base64' -or $data.type -ne 'file') { throw 'Arquivo de configuracao remoto invalido.' }
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($data.content)) | ConvertFrom-Json
}

function Assert-Device($Allowlist, [string]$Hash) {
    if ($Allowlist.schema -ne 1) { throw 'Lista de computadores incompatível.' }
    $matches = @($Allowlist.devices | Where-Object { $_.hash -ceq $Hash })
    if ($matches.Count -ne 1 -or $matches[0].enabled -isnot [bool] -or $matches[0].enabled -ne $true) { throw "Computador ainda nao autorizado. Envie este hash ao Daniel: $Hash" }
}

function Expand-CheckedZip([string]$Archive, [string]$Destination, [long]$Maximum = 4294967296) {
    Assert-NoLinks $Destination
    [IO.Directory]::CreateDirectory($Destination) | Out-Null
    if ([IO.Directory]::EnumerateFileSystemEntries($Destination).GetEnumerator().MoveNext()) { throw 'Destino de extracao deve estar vazio.' }
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    try {
        if ($zip.Entries.Count -gt 30000) { throw 'ZIP com arquivos demais.' }
        $expanded = 0L
        foreach ($entry in $zip.Entries) {
            $expanded += $entry.Length
            if ($expanded -gt $Maximum -or (($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960) { throw 'ZIP com link ou tamanho invalido.' }
            $relative = $entry.FullName.Replace('/', '\').TrimEnd('\')
            if (-not $relative) { continue }
            $target = Get-SafeChild $Destination $relative
            if (-not $seen.Add($relative)) { throw 'ZIP com caminhos duplicados.' }
            if ($entry.FullName.EndsWith('/') -or $entry.FullName.EndsWith('\')) { [IO.Directory]::CreateDirectory($target) | Out-Null; continue }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
            $inputStream = $entry.Open()
            $outputStream = [IO.File]::Open($target,[IO.FileMode]::CreateNew)
            try { $inputStream.CopyTo($outputStream) } finally { $outputStream.Dispose(); $inputStream.Dispose() }
        }
    } finally { $zip.Dispose() }
}

function Assert-FileManifest([string]$Root, $Files) {
    if (-not $Files -or @($Files.PSObject.Properties).Count -eq 0) { throw 'Manifesto vazio.' }
    $checked = 0; $totalFiles = @($Files.PSObject.Properties).Count; $last = [DateTime]::UtcNow
    foreach ($item in $Files.PSObject.Properties) {
        if ($item.Value -notmatch '^[a-f0-9]{64}$') { throw 'Hash invalido no manifesto.' }
        $path = Get-SafeChild $Root $item.Name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Digest $path) -cne $item.Value) { throw ('Arquivo incompleto/alterado: '+$item.Name) }
        $checked++
        if (([DateTime]::UtcNow-$last).TotalSeconds -ge 4) {
            Write-Host ("`rConferindo ambiente: $checked/$totalFiles arquivos           ") -NoNewline
            $last = [DateTime]::UtcNow
        }
    }
    Write-Host ("`rAmbiente conferido: $checked/$totalFiles arquivos.           ")
}

function Get-GitBlobHash([string]$Path) {
    $data = [IO.File]::ReadAllBytes($Path)
    $header = [Text.Encoding]::ASCII.GetBytes(('blob '+$data.Length+"`0"))
    $sha = [Security.Cryptography.SHA1]::Create()
    try {
        $sha.TransformBlock($header,0,$header.Length,$header,0) | Out-Null
        $sha.TransformFinalBlock($data,0,$data.Length) | Out-Null
        return [BitConverter]::ToString($sha.Hash).Replace('-','').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Assert-GitTree([string]$Root, $Tree) {
    if ($Tree.truncated -or -not $Tree.tree) { throw 'Arvore Git incompleta.' }
    $names = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($item in $Tree.tree) {
        if ($item.type -eq 'tree') { continue }
        if ($item.type -ne 'blob' -or $item.mode -notin @('100644','100755') -or $item.sha -notmatch '^[a-f0-9]{40}$') { throw 'Objeto Git/link nao permitido.' }
        if (-not $names.Add($item.path)) { throw 'Caminho Git ambiguo.' }
        $path = Get-SafeChild $Root $item.path
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-GitBlobHash $path) -cne $item.sha) { throw ('Arquivo difere do GitHub: '+$item.path) }
    }
    if (@(Get-ChildItem -LiteralPath $Root -Force -Recurse -File).Count -ne $names.Count) { throw 'Pacote contem arquivos extras ou faltantes.' }
}

function Remove-OwnSession([string]$Session, [string]$Base) {
    $path = [IO.Path]::GetFullPath($Session)
    $root = [IO.Path]::GetFullPath($Base).TrimEnd('\')
    if (-not $path.StartsWith($root+'\preedit-',[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetDirectoryName($path) -ne $root) { throw 'Limpeza fora da sessao recusada.' }
    Assert-NoLinks $path
    if (Test-Path -LiteralPath $path) {
        if (Get-ChildItem -LiteralPath $path -Force -Recurse | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Link na sessao temporaria; limpeza automatica recusada.' }
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

function Install-VisualRuntime([string]$Session) {
    Write-Host 'Falta uma DLL do ambiente. O Windows pode pedir autorizacao para o Visual C++ oficial.' -ForegroundColor Yellow
    $answer = Read-Host 'Instalar Microsoft Visual C++ Runtime? [s/N]'
    if ($answer -notmatch '^(?i:s|sim|yes|y)$') { throw 'Preparacao interrompida; nenhuma protecao do Windows foi alterada.' }
    $file = Join-Path $Session 'vc_redist.x64.exe'
    Get-RemoteFile 'https://aka.ms/vs/17/release/vc_redist.x64.exe' $file '' 104857600 'application/octet-stream' $true
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
    $signature = Get-AuthenticodeSignature -LiteralPath $file
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { throw 'Assinatura Microsoft nao confirmada.' }
    $process = Start-Process -FilePath $file -ArgumentList '/install','/quiet','/norestart' -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -notin @(0,3010)) { throw 'Instalacao do Visual C++ cancelada/falhou.' }
    if ($process.ExitCode -eq 3010) { Write-Host 'O Windows recomenda reiniciar depois desta instalacao.' -ForegroundColor Yellow }
}

function Invoke-PreEditor {
    $exitCode = 1; $session = $null; $lock = $null; $token = $null
    try {
        $nativeArch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        if ($env:OS -ne 'Windows_NT' -or [Environment]::Is64BitOperatingSystem -ne $true -or $nativeArch -ne 'AMD64') { throw 'Use Windows 10/11 x64 (nao ARM ou Mac).' }
        $base = Split-Path -Parent $env:PREEDITOR_BOOTSTRAP
        if ($base.StartsWith('\\')) { throw 'Coloque o BAT em uma pasta local sua.' }
        $edition = Join-Path $base 'Edição'
        Assert-NoLinks $edition
        $metadata = Join-Path $edition 'Metadados'
        $clients = Join-Path $edition 'Clientes'
        foreach ($path in @($metadata,$clients,(Join-Path $metadata 'Acesso'),(Join-Path $metadata 'Temporarios'),(Join-Path $metadata 'Runtime'))) { Assert-NoLinks $path; [IO.Directory]::CreateDirectory($path) | Out-Null }
        $access = Join-Path $metadata 'Acesso'
        $tempBase = Join-Path $metadata 'Temporarios'
        $lock = [IO.File]::Open((Join-Path $metadata 'execucao.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        $hash = Get-ComputerFingerprint
        $register = Join-Path $access 'COMPUTADOR.md'
        $first = -not (Test-Path -LiteralPath $register)
        [IO.File]::WriteAllText($register, "# Cadastro do computador`n`nHash: ``$hash```n`nEnvie somente este hash ao Daniel para autorizar. Nao envie chaves ou senhas.`n", [Text.Encoding]::UTF8)
        Write-Host 'Pre Editor Premiere | Daniel Fileto' -ForegroundColor Cyan
        if ($env:PREEDITOR_MODE -eq '--cadastro' -or ($first -and -not $env:PREEDITOR_GITHUB_TOKEN)) {
            Write-Host "`nHASH DO COMPUTADOR:`n$hash" -ForegroundColor Yellow
            Write-Host "`nCadastro criado em Edição\Metadados\Acesso\COMPUTADOR.md. Envie o hash ao Daniel e execute novamente apos autorizar."
            $exitCode = 3; return
        }
        $session = Join-Path $tempBase ('preedit-'+[Guid]::NewGuid().ToString('N'))
        [IO.Directory]::CreateDirectory($session) | Out-Null
        $replace = $env:PREEDITOR_MODE -eq '--configurar'
        $token = if ($env:PREEDITOR_GITHUB_TOKEN) { $env:PREEDITOR_GITHUB_TOKEN } else { Read-Secret $access 'github' 'Token GitHub de leitura (oculto)' $replace }
        Write-Host '[1/4] Conferindo acesso e versao atual...' -ForegroundColor Yellow
        $commit = Get-RemoteJson ($script:Api+'/commits/main') $token $session
        if ($commit.sha -notmatch '^[a-f0-9]{40}$') { throw 'Versao GitHub invalida.' }
        Assert-Device (Get-RepositoryJson 'acesso/computadores.json' $commit.sha $token $session) $hash
        $release = Get-RepositoryJson 'online/versao.json' $commit.sha $token $session
        if ($release.schema -ne 1 -or $release.editor -ne 'premiere' -or $release.bootstrap_version -gt 1) { throw 'Versao de motor incompatível; baixe o BAT atualizado.' }
        $codeZip = Join-Path $session 'motor.zip'
        Get-RemoteFile ($script:Api+'/zipball/'+$commit.sha) $codeZip $token 20971520 'application/vnd.github+json'
        $sourceBase = Join-Path $session 'fonte'
        Expand-CheckedZip $codeZip $sourceBase 104857600
        $roots = @(Get-ChildItem -LiteralPath $sourceBase -Directory)
        if ($roots.Count -ne 1) { throw 'Estrutura do pacote remoto invalida.' }
        $source = $roots[0].FullName
        # Os hashes vêm da árvore Git do commit: editar prompt/código no GitHub não exige atualizar uma lista manual.
        Assert-GitTree $source (Get-RemoteJson ($script:Api+'/git/trees/'+$commit.sha+'?recursive=1') $token $session)
        $runtimeSpec = [IO.File]::ReadAllText((Join-Path $source 'online\runtime.json'),[Text.Encoding]::UTF8) | ConvertFrom-Json
        if ($runtimeSpec.version -notmatch '^windows-x64-v[0-9]+$' -or $runtimeSpec.tag -cne $runtimeSpec.version -or $runtimeSpec.asset -notmatch '^preedit-runtime-windows-x64-v[0-9]+\.zip$' -or $runtimeSpec.sha256 -notmatch '^[a-f0-9]{64}$' -or $runtimeSpec.size -le 0 -or $runtimeSpec.size -gt 1GB) { throw 'Descritor de ambiente invalido.' }
        $runtime = Get-SafeChild (Join-Path $metadata 'Runtime') $runtimeSpec.version
        Write-Host '[2/4] Preparando Python, bibliotecas e FFmpeg...' -ForegroundColor Yellow
        if (-not (Test-Path -LiteralPath $runtime)) {
            if ((New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($metadata))).AvailableFreeSpace -lt 5GB) { throw 'Reserve pelo menos 5 GB livres para instalar.' }
            $assets = Get-RemoteJson ($script:Api+'/releases/tags/'+$runtimeSpec.tag) $token $session
            $asset = @($assets.assets | Where-Object { $_.name -ceq $runtimeSpec.asset })
            if ($asset.Count -ne 1 -or $asset[0].size -ne $runtimeSpec.size) { throw 'Ambiente ainda nao publicado ou tamanho divergente.' }
            $runtimeZip = Join-Path $session 'runtime.zip'
            Get-RemoteFile ($script:Api+'/releases/assets/'+$asset[0].id) $runtimeZip $token 1073741824 'application/octet-stream'
            if ((Get-Digest $runtimeZip) -cne $runtimeSpec.sha256) { throw 'Hash do ambiente divergente; nada sera executado.' }
            $runtimeStage = Join-Path $session 'runtime'
            Expand-CheckedZip $runtimeZip $runtimeStage
            Assert-FileManifest $runtimeStage $runtimeSpec.files
            Assert-NoLinks $runtime
            Move-Item -LiteralPath $runtimeStage -Destination $runtime
        } else {
            Assert-FileManifest $runtime $runtimeSpec.files
            Write-Host 'Ambiente no cache; sem novo download.' -ForegroundColor Green
        }
        $python = Join-Path $runtime 'python\python.exe'
        $env:PYTHONUTF8 = '1'; $env:PYTHONUNBUFFERED = '1'; $env:PYTHONNOUSERSITE = '1'; $env:PYTHONDONTWRITEBYTECODE = '1'
        $env:PYTHONHOME = $null; $env:PYTHONPATH = $null
        $env:PATH = (Join-Path $runtime 'ffmpeg\bin')+';'+$env:PATH
        $env:PIPELINE_COLOR = '1'
        $env:GEMINI_API_KEY = if ($env:GEMINI_API_KEY) { $env:GEMINI_API_KEY } else { Read-Secret $access 'gemini' 'Chave Gemini (oculta; guardada criptografada neste Windows)' $replace }
        $entry = Join-Path $source 'online\executar_online.py'
        $arguments = @('-s','-B','-u',$entry,'--metadata',$metadata,'--clients',$clients)
        $prepare = $arguments+@('--preparar')
        if ($env:PREEDITOR_MODE -eq '--verificar') { $prepare += '--verificar' }
        Write-Host '[3/4] Conferindo modelo de transcricao e referencias...' -ForegroundColor Yellow
        & $python @prepare
        if ($LASTEXITCODE -in @(20,-1073741515)) { Install-VisualRuntime $session; & $python @prepare }
        if ($LASTEXITCODE -ne 0) { throw 'Preparacao nao terminou. Confira a mensagem acima; originais preservados.' }
        $hdr = Get-Content -LiteralPath (Join-Path $metadata 'Runtime\hdr-conferido.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $hdr.available -and $env:PREEDITOR_MODE -ne '--verificar') {
            $answer = Read-Host 'Conversao HDR indisponivel. Continuar somente com videos SDR? [s/N]'
            if ($answer -notmatch '^(?i:s|sim|yes|y)$') { throw 'Execucao cancelada ate corrigir o driver HDR.' }
        }
        # Nova consulta online apos os downloads longos; sem cache de autorizacao.
        Assert-Device (Get-RepositoryJson 'acesso/computadores.json' 'main' $token $session) $hash
        $token = $null
        if ($env:PREEDITOR_MODE -eq '--verificar') { $arguments += '--verificar' }
        Write-Host '[4/4] Iniciando pre-edicao...' -ForegroundColor Yellow
        & $python @arguments
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) { Write-Host 'Concluido. Projetos e relatorios em Edição\Metadados\Trabalhos.' -ForegroundColor Green }
    } catch {
        Write-Host ('ERRO: '+$_.Exception.Message) -ForegroundColor Red
        $exitCode = 1
    } finally {
        $env:GEMINI_API_KEY = $null; $token = $null
        if ($session) {
            try { Remove-OwnSession $session $tempBase; Write-Host 'Codigo temporario removido; clientes, projetos e caches preservados.' }
            catch { Write-Host 'Nao foi possivel concluir a limpeza temporaria. Feche os processos e confira Metadados\Temporarios.' -ForegroundColor Yellow; if ($exitCode -eq 0) { $exitCode = 1 } }
        }
        if ($lock) { $lock.Dispose() }
        $script:ExitCode = $exitCode
    }
}

if ($env:PREEDITOR_TEST_IMPORT -ne '1') {
    Invoke-PreEditor
    Write-Host ''
    Read-Host 'Pressione ENTER para sair' | Out-Null
    exit $script:ExitCode
}
