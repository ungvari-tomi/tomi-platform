<# Run locally edited dbt models using the Terraform-built, lock-aware runtime. #>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('check', 'parse', 'build')]
    [string]$Action = 'parse',
    [string]$ProjectDirectory,
    [string]$ArtifactDirectory,
    [string]$WarehouseVolume = 'retailflow-duckdb-data',
    [string]$Image = 'retailflow-dbt:local'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
if (-not $ProjectDirectory) {
    $ProjectDirectory = Join-Path $workspaceRoot 'infra/dbt/project'
}
if (-not $ArtifactDirectory) {
    $ArtifactDirectory = Join-Path $workspaceRoot 'artifacts/dbt-dev'
}
$projectPath = (Resolve-Path -LiteralPath $ProjectDirectory).Path
foreach ($filename in @('dbt_project.yml', 'profiles.yml')) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectPath $filename) -PathType Leaf)) {
        throw "Missing $filename in $projectPath"
    }
}

# Preflight prevents Docker from silently creating an empty warehouse volume.
$null = & docker volume inspect $WarehouseVolume --format '{{.Name}}'
if ($LASTEXITCODE -ne 0) {
    throw "Warehouse volume '$WarehouseVolume' is missing. Apply the local Terraform setup first."
}
$imageId = & docker image inspect $Image --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) {
    throw "Image '$Image' is missing. Apply the local Terraform setup first."
}
$null = New-Item -ItemType Directory -Path $ArtifactDirectory -Force
$artifactPath = (Resolve-Path -LiteralPath $ArtifactDirectory).Path

$containerName = 'retailflow-dbt-dev-' + [guid]::NewGuid().ToString('N')
$dockerArguments = @(
    'run', '--name', $containerName,
    '--network', 'none', '--read-only', '--cap-drop', 'ALL',
    '--security-opt', 'no-new-privileges:true', '--memory', '512m', '--cpus', '1',
    '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m,uid=10001,gid=10001',
    '--mount', "type=volume,source=$WarehouseVolume,target=/warehouse",
    '--mount', "type=bind,source=$projectPath,target=/app/project,readonly",
    '--mount', 'type=volume,target=/artifacts',
    $imageId, $Action
)
Write-Host "Running dbt $Action using $projectPath"
Write-Host "Warehouse volume: $WarehouseVolume | Local artifacts: $artifactPath"
try {
    & docker @dockerArguments
    $dbtExit = $LASTEXITCODE
    # Docker copies artifacts as the host caller, avoiding Linux UID bind-mount conflicts.
    & docker cp ($containerName + ':/artifacts/.') $artifactPath
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not preserve dbt artifacts on the host.'
    }
} finally {
    # Only this UUID-named invocation and its anonymous artifact volume are removed.
    # The pre-existing warehouse is mounted by explicit volume name and is retained.
    & docker rm --volumes $containerName | Out-Null
}
exit $dbtExit

