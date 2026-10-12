param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$IdaExecutable
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'benchmark_qt.py'
$process = Start-Process -FilePath $IdaExecutable -ArgumentList "-t -A -S`"$scriptPath`"" -WorkingDirectory $PSScriptRoot -Wait -PassThru
[pscustomobject]@{ Id = $process.Id; ExitCode = $process.ExitCode }
exit $process.ExitCode
