[CmdletBinding()]
param(
    [Alias("Host")]
    [ValidateSet("codex", "claude-code", "cursor", "gemini-cli")]
    [string]$TargetHost = "codex",
    [string]$PythonCommand = "python",
    [string]$DataDirectory = $(if ($env:BLUEPRINT_WORKFLOW_DATA_DIR) { $env:BLUEPRINT_WORKFLOW_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "BlueprintWorkflowPlugin" }),
    [string]$ConfigRoot,
    [string]$Provider,
    [string]$Model,
    [Alias("SkipDependencies")]
    [switch]$SkipDependencyInstall,
    [switch]$StageOnly,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
& $PythonCommand -c "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ is required'"
if ($LASTEXITCODE -ne 0) { throw "A working Python 3.12+ is required." }
$Launcher = Join-Path $PSScriptRoot "launch.py"
$InstallArguments = @($Launcher, "--data-dir", $DataDirectory, "install", "--host", $TargetHost)
if ($ConfigRoot) { $InstallArguments += @("--config-root", $ConfigRoot) }
if ($Provider) { $InstallArguments += @("--provider", $Provider) }
if ($Model) { $InstallArguments += @("--model", $Model) }
if ($SkipDependencyInstall) { $InstallArguments += "--skip-dependencies" }
if ($StageOnly) { $InstallArguments += "--stage-only" }
if ($DryRun) { $InstallArguments += "--dry-run" }
& $PythonCommand @InstallArguments
if ($LASTEXITCODE -ne 0) { throw "The installer reported an error; review its structured result before retrying." }
