@echo off
setlocal EnableExtensions
chcp 65001 >nul
title ZCode Start Plan JWT account converter

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command ^
"$ErrorActionPreference = 'Stop'; ^
try { ^
  $dir = [IO.Path]::GetFullPath('%~dp0'); ^
  $output = Join-Path $dir 'zcode-accounts.json'; ^
  $files = @(Get-ChildItem -LiteralPath $dir -File -Filter '*.zcas.json'); ^
  if ($files.Count -eq 0) { throw 'No *.zcas.json files were found beside this script.' } ^
  $seen = New-Object 'System.Collections.Generic.HashSet[string]'; ^
  $converted = New-Object 'System.Collections.Generic.List[object]'; ^
  $jwtCount = 0; ^
  foreach ($file in $files) { ^
    try { ^
      $source = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($file.FullName, [Text.Encoding]::UTF8)); ^
      foreach ($account in @($source.accounts)) { ^
        $config = ConvertFrom-Json -InputObject ([string]$account.snapshot.config); ^
        $secret = [string]$config.provider.'builtin:zai-start-plan'.options.apiKey; ^
        if ([string]::IsNullOrWhiteSpace($secret)) { continue } ^
        if (($secret -split '\.').Count -ne 3) { continue } ^
        if (-not $seen.Add($secret)) { continue } ^
        $name = [string]$account.meta.email; ^
        if ([string]::IsNullOrWhiteSpace($name)) { $name = [string]$account.meta.label } ^
        if ([string]::IsNullOrWhiteSpace($name)) { $name = 'zai-account-' + ($converted.Count + 1) } ^
        $converted.Add([pscustomobject][ordered]@{ name = $name; mode = 'jwt'; secret = $secret }); ^
        $jwtCount++; ^
      } ^
    } catch { ^
      Write-Warning ('Skipped ' + $file.Name + ': ' + $_.Exception.Message); ^
    } ^
  } ^
  if ($converted.Count -eq 0) { throw 'No usable ZCode Start Plan JWT was found in the input file(s).' } ^
  $payload = [ordered]@{ ^
    version = 1; ^
    exported_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0; ^
    providers = [ordered]@{ zai = [object[]]$converted; bigmodel = @() } ^
  }; ^
  $json = ConvertTo-Json -InputObject $payload -Depth 10; ^
  [IO.File]::WriteAllText($output, $json, (New-Object Text.UTF8Encoding($false))); ^
  Write-Host ('Created: ' + $output) -ForegroundColor Green; ^
  Write-Host ('Accounts: ' + $converted.Count) -ForegroundColor Green; ^
  Write-Host ('JWT mode: ' + $jwtCount) -ForegroundColor Green; ^
  Write-Host 'Existing zcode-accounts.json was overwritten.' -ForegroundColor Yellow; ^
} catch { ^
  Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red; ^
  exit 1; ^
}"

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" echo Conversion failed. Please check the message above.
pause
exit /b %EXIT_CODE%
