param([ValidateSet("start","prepare","test","password","stop")][string]$Action="start")
$ErrorActionPreference="Stop"
Set-Location -LiteralPath $PSScriptRoot
$projectRoot=$PSScriptRoot
$env:AETHERRAG_ENV_FILE=Join-Path $projectRoot ".env"
function Invoke-DockerChecked {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker failed (exit $LASTEXITCODE)." }
}
function Invoke-ComposeChecked {
    Invoke-DockerChecked compose -p aetherrag-evaluation --env-file $env:AETHERRAG_ENV_FILE @args
}
function Prepare-Local {
    Invoke-DockerChecked info
    Invoke-DockerChecked build -t aetherrag-app:local .
    if (-not (Test-Path -LiteralPath $env:AETHERRAG_ENV_FILE)) {
        Invoke-DockerChecked run --rm -it --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /tmp:rw,size=64m --user 0:0 --mount "type=bind,source=$projectRoot,target=/setup" --workdir /setup --entrypoint python aetherrag-app:local /workspace/backend/scripts/setup_env.py --output /setup/.env
    }
    Invoke-ComposeChecked config --quiet
}
switch ($Action) {
    "prepare" { Prepare-Local }
    "start" { Prepare-Local; Invoke-ComposeChecked up -d --no-build --wait --wait-timeout 240 }
    "stop" { Invoke-ComposeChecked stop }
    "password" {
        if (-not (Test-Path -LiteralPath $env:AETHERRAG_ENV_FILE)) { throw "Run start first." }
        Invoke-DockerChecked run --rm -it --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /tmp:rw,size=64m --user 0:0 --mount "type=bind,source=$projectRoot,target=/setup" --workdir /setup --entrypoint python aetherrag-app:local /workspace/backend/scripts/change_operator_password.py --env-file /setup/.env
        Invoke-ComposeChecked up -d --no-deps --force-recreate --wait --wait-timeout 240 api worker
    }
    "test" {
        $localPassword=Read-Host "Local application password" -AsSecureString
        $passwordPointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($localPassword)
        try {
            $env:STACK_TEST_PASSWORD=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
            if (-not $env:STACK_TEST_PASSWORD) { throw "Password is required." }
            Invoke-ComposeChecked --profile test run --rm integration-tests
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
            Remove-Item Env:\STACK_TEST_PASSWORD -ErrorAction SilentlyContinue
        }
    }
}
