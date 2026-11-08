# End to end inside the image, from Windows PowerShell: init, backup, verify, rotate, recovery key, restore.
# Needs Docker Desktop. Usage: .\tools\docker_e2e.ps1 [-Image encbackup]
param([string]$Image = "encbackup")
$ErrorActionPreference = "Stop"

$id = Get-Random
$vol = "encbackup-e2e-$id"
$work = Join-Path ([IO.Path]::GetTempPath()) "encbackup-e2e-$id"
New-Item -ItemType Directory -Path "$work\data\sub" | Out-Null
Set-Content -Path "$work\data\hello.txt" -Value "hello" -Encoding ascii
$bytes = New-Object byte[] 200000
(New-Object Random).NextBytes($bytes)
[IO.File]::WriteAllBytes("$work\data\sub\random.bin", $bytes)

function Invoke-Enc {
    docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp -v "${vol}:/state" -v "${work}\data:/data:ro" $Image @args
    if ($LASTEXITCODE -ne 0) { throw "encbackup $args failed (exit $LASTEXITCODE)" }
}

try {
    # the recovery identity is made inside the volume (mode 0600); in real life make it elsewhere
    $out = docker run --rm -v "${vol}:/state" --entrypoint sh $Image -c "umask 077 && age-keygen -o /state/recovery.txt 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "age-keygen failed" }
    if (-not (($out | Out-String) -match "age1[a-z0-9]{58}")) { throw "no public key in: $out" }
    $pub = $Matches[0]

    Invoke-Enc init --recovery-recipient $pub
    Invoke-Enc backup --verify
    Invoke-Enc verify --json
    Invoke-Enc rotate --new-identity
    Invoke-Enc verify --recovery-identity /state/recovery.txt
    Invoke-Enc status
    Invoke-Enc restore --target /state/restored
    docker run --rm -v "${vol}:/state:ro" -v "${work}\data:/data:ro" --entrypoint sh $Image -c "diff -r /data /state/restored/data && echo restored tree is identical"
    if ($LASTEXITCODE -ne 0) { throw "the restored tree differs" }
    Write-Host "docker end-to-end: OK"
}
finally {
    docker volume rm -f $vol | Out-Null
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
