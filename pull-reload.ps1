param(
	[string]$HostName = "server",
	[string]$UserName = "server",
	[string]$RemotePath = "~/Documents/GitHub/VStreamware/",
	[string]$GitRemote = "origin",
	[string]$GitBranch = "main",
	[string]$ServiceName = "VStreamware.service",
	[string]$Password = "evergreen",
	[string]$SudoPassword = ""
)

$ErrorActionPreference = "Stop"

function ConvertTo-ShellSingleQuoted {
	param([string]$Value)
	$escaped = $Value.Replace("'", "'\''")
	return "'$escaped'"
}

if ([string]::IsNullOrWhiteSpace($Password)) {
	Write-Error "Password cannot be empty for non-interactive mode."
	exit 1
}

if ([string]::IsNullOrWhiteSpace($RemotePath)) {
	Write-Error "RemotePath cannot be empty."
	exit 1
}

if ([string]::IsNullOrWhiteSpace($GitRemote) -or [string]::IsNullOrWhiteSpace($GitBranch)) {
	Write-Error "GitRemote and GitBranch cannot be empty."
	exit 1
}

if ([string]::IsNullOrWhiteSpace($ServiceName)) {
	Write-Error "ServiceName cannot be empty."
	exit 1
}

if ([string]::IsNullOrWhiteSpace($SudoPassword)) {
	$SudoPassword = $Password
}

$cdCommand = if ($RemotePath.StartsWith("~/")) {
	"cd $RemotePath"
}
else {
	"cd $(ConvertTo-ShellSingleQuoted $RemotePath)"
}

$sudoPasswordArg = ConvertTo-ShellSingleQuoted $SudoPassword

$remoteCommand = @(
	"set -e",
	$cdCommand,
	"pwd",
	"echo '[INFO] Pulling latest from $GitRemote/$GitBranch...'",
	"git pull $GitRemote $GitBranch",
	"echo '[INFO] Restarting $ServiceName...'",
	"printf '%s\n' $sudoPasswordArg | sudo -S -p '' systemctl restart $ServiceName",
	"echo '[INFO] Service state:'",
	"systemctl is-active $ServiceName",
	"echo '[OK] Git pull + service restart completed'"
) -join "; "

$target = "$UserName@$HostName"

$plinkCommand = Get-Command plink -ErrorAction SilentlyContinue

if ($plinkCommand) {
	Write-Host "Connecting with plink to $target..."
	& $plinkCommand.Source -batch -ssh $target -pw $Password $remoteCommand
}
else {
	Write-Host "plink not found. Falling back to OpenSSH in batch mode (key auth only)."
	& ssh -o BatchMode=yes $target $remoteCommand
}

if ($LASTEXITCODE -eq 0) {
	Write-Host "[OK] SSH command completed successfully."
}
else {
	Write-Error "SSH command failed with exit code $LASTEXITCODE"
	exit $LASTEXITCODE
}
