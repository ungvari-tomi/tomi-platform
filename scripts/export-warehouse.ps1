<# Export a consistent, verified DuckDB snapshot for DBeaver. #>
[CmdletBinding()]
param([string]$OutputDirectory)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $workspaceRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw 'Project Python environment is missing. Complete the environment setup first.'
}
$arguments = @('-m', 'scripts.export_warehouse')
if ($OutputDirectory) {
    # Resolve relative output paths against the caller's directory before changing location.
    $arguments += @('--output-directory', $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory))
}
Push-Location -LiteralPath $workspaceRoot
try {
    & $pythonExecutable @arguments
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode
