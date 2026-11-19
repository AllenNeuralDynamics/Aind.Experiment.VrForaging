$scriptPath = $MyInvocation.MyCommand.Path
$scriptDirectory = Split-Path -Parent $scriptPath
Set-Location (Split-Path -Parent $scriptDirectory)

Write-Output "Initializing and updating submodules..."
&git submodule update --init --recursive

$repoRoot = (Get-Location).Path

# Each setup script runs in its own job (separate process) so they execute in parallel.
$setupScripts = @(
    @{ Name = "VrForaging"; Path = "./Aind.Behavior.VrForaging/scripts/deploy.ps1"; Args = @() },
    @{ Name = "Fip"; Path = "./Aind.Physiology.Fip/scripts/deploy.ps1"; Args = @() },
    @{ Name = "Olfactometer"; Path = "./Aind.Behavior.Device.Olfactometer/.bonsai/setup.ps1"; Args = @() },
    @{ Name = "OpenEphys"; Path = "./Aind.Physiology.OpenEphys/.open-ephys/setup.ps1"; Args = @("-NoLaunch") }
)

Write-Output "Running setup scripts in parallel..."
$jobs = foreach ($s in $setupScripts) {
    Start-Job -Name $s.Name -ArgumentList $repoRoot, $s.Path, $s.Args -ScriptBlock {
        param($root, $path, $scriptArgs)
        Set-Location $root
        & $path @scriptArgs
        if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "Exited with code $LASTEXITCODE" }
    }
}

Write-Output "Creating a Python  environment..."

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "The 'uv' command was not found. See https://docs.astral.sh/uv/getting-started/installation/ for instructions."
}

if (Test-Path -Path ./.venv) {
    Remove-Item ./.venv -Recurse -Force
}
&uv venv
.\.venv\Scripts\Activate.ps1
Write-Output "Synchronizing environment..."
&uv sync

Write-Output "Waiting for setup scripts to finish..."
$failed = @()
foreach ($job in $jobs) {
    Wait-Job $job | Out-Null
    Write-Output "===== $($job.Name) ($($job.State)) ====="
    Receive-Job $job -ErrorAction Continue
    if ($job.State -ne "Completed") { $failed += $job.Name }
    Remove-Job $job
}
if ($failed.Count -gt 0) {
    throw "Setup failed for: $($failed -join ', ')"
}
