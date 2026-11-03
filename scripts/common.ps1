$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'

function Invoke-Az {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $result = & az @Arguments --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed: az $($Arguments[0]) $($Arguments[1]) (exit $LASTEXITCODE)"
    }
    return ($result -join "`n")
}

function Write-JsonFile {
    param($Value, [string]$Path)
    $json = $Value | ConvertTo-Json -Depth 30
    [System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Remove-LegacyMcpOperations {
    param($State)
    if (-not $State.outputs.apimName.value) { return }
    $base = "https://management.azure.com/subscriptions/$($State.subscriptionId)/resourceGroups/$($State.resourceGroup)/providers/Microsoft.ApiManagement/service/$($State.outputs.apimName.value)/apis/mcp/operations"
    $operations = Invoke-Az @('rest', '--method', 'GET', '--url', "${base}?api-version=2024-05-01", '-o', 'json') | ConvertFrom-Json
    foreach ($operation in $operations.value) {
        if ($operation.name -notin @('mcp-post', 'mcp-get', 'mcp-delete', 'health', 'metadata')) {
            throw "Unexpected operation $($operation.name); refusing automatic migration."
        }
    }
    foreach ($operation in $operations.value) {
        Invoke-Az @('rest', '--method', 'DELETE', '--url', "$base/$($operation.name)?api-version=2024-05-01", '-o', 'none') | Out-Null
    }
}

function Start-PrivateContainer {
    param($State, [string]$Name, [string]$Module, [array]$Environment, [string]$IdentityId)
    if (-not $IdentityId) { throw 'An explicit runner managed identity is required.' }
    $spec = @{
        apiVersion = '2023-05-01'
        name = $Name
        location = $State.location
        identity = @{
            type = 'UserAssigned'
            userAssignedIdentities = @{ $IdentityId = @{} }
        }
        properties = @{
            osType = 'Linux'
            restartPolicy = 'Never'
            subnetIds = @(@{ id = $State.outputs.probeSubnetId.value })
            imageRegistryCredentials = @(@{
                server = $State.outputs.registryServer.value
                identity = $IdentityId
            })
            containers = @(@{
                name = 'runner'
                properties = @{
                    image = "$($State.outputs.registryServer.value)/mcpdemo:$($State.imageTag)"
                    command = @('python', '-m', $Module)
                    environmentVariables = $Environment
                    resources = @{ requests = @{ cpu = 1; memoryInGB = 1.5 } }
                }
            })
        }
    }
    $path = Join-Path ([System.IO.Path]::GetTempPath()) ("mcp-" + [guid]::NewGuid().ToString('N') + '.json')
    Write-JsonFile $spec $path
    try {
        Invoke-Az @('container', 'create', '-g', $State.resourceGroup, '--file', $path, '-o', 'none') | Out-Null
        for ($i = 0; $i -lt 90; $i++) {
            $container = Invoke-Az @('container', 'show', '-g', $State.resourceGroup, '-n', $Name, '-o', 'json') | ConvertFrom-Json
            $status = $container.containers[0].instanceView.currentState
            if ($status.state -eq 'Terminated') {
                Invoke-Az @('container', 'logs', '-g', $State.resourceGroup, '-n', $Name)
                if ($status.exitCode -ne 0) { throw "$Module failed with exit code $($status.exitCode)" }
                return
            }
            Start-Sleep -Seconds 10
        }
        throw "Timed out waiting for $Module; inspect the container and its logs."
    }
    finally {
        # A token may be present in secureValue fields; never retain the request file.
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
    }
}
