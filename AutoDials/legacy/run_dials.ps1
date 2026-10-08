param(
    [Parameter(Mandatory=$true)][string]$RunDirectory,
    [Parameter(Mandatory=$true)][string]$Command,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$DialsArguments,
    [string]$DialsRoot = 'C:\dials'
)
# Run executables directly: CMD launchers cannot retain a WSL UNC working directory.
$oldPath = $env:PATH
try {
    $env:PATH = "$DialsRoot;$DialsRoot\Library\bin;$DialsRoot\Scripts;$DialsRoot\bin;$env:PATH"
    Push-Location -LiteralPath $RunDirectory
    try {
        & "$DialsRoot\python.exe" "$PSScriptRoot\..\dials_compat.py" $Command @DialsArguments
        if ($LASTEXITCODE -ne 0) { throw "dials.$Command failed: $LASTEXITCODE" }
    } finally { Pop-Location }
} finally { $env:PATH = $oldPath }
