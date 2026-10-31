# 核对发布包里每个文件的 SHA-256（与 SHA256SUMS 一致才可安装）。用法：在发布包目录里运行  powershell -ExecutionPolicy Bypass -File .\verify.ps1
$root = $PSScriptRoot
$bad = 0
$count = 0
foreach ($line in Get-Content -LiteralPath (Join-Path $root 'SHA256SUMS') -Encoding UTF8) {
    if (-not $line) { continue }
    $hash, $rel = $line -split '  ', 2
    $count++
    $path = Join-Path $root $rel
    if (-not (Test-Path -LiteralPath $path)) { Write-Host "缺少：$rel"; $bad++; continue }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower() -ne $hash) { Write-Host "内容不一致：$rel"; $bad++ }
}
if ($bad) { Write-Host "校验失败：$bad 个文件有问题（共 $count 个）。不要安装，重新拷贝发布包。"; exit 1 }
Write-Host "校验通过：$count 个文件全部一致。"
exit 0
