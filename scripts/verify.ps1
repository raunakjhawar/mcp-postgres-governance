[CmdletBinding()]
param([switch]$NetworkOnly)
. "$PSScriptRoot\common.ps1"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$state = Get-Content -Raw "$root\.deployment\state.json" | ConvertFrom-Json
if (-not $state.outputs.apimName.value) {
    throw 'Saved deployment does not include APIM. Complete deploy.ps1 before verification.'
}
$reportPath = "$root\.deployment\verification.json"
if (Test-Path -LiteralPath $reportPath) { Remove-Item -LiteralPath $reportPath }
$account = Invoke-Az @('account', 'show', '-o', 'json') | ConvertFrom-Json
if ($account.id -ne $state.subscriptionId -or $account.tenantId -ne $state.tenantId) {
    throw 'Azure CLI context differs from saved deployment.'
}
$database = Invoke-Az @('postgres', 'flexible-server', 'show', '-g', $state.resourceGroup, '-n', $state.outputs.postgresName.value, '-o', 'json') | ConvertFrom-Json
if ($database.network.publicNetworkAccess -ne 'Disabled' -or $database.authConfig.passwordAuth -ne 'Disabled') {
    throw 'PostgreSQL must have public network access and password authentication disabled.'
}
Write-Host 'PASS PostgreSQL has private-only networking and Entra-only authentication'
$apim = Invoke-Az @('apim', 'show', '-g', $state.resourceGroup, '-n', $state.outputs.apimName.value, '-o', 'json') | ConvertFrom-Json
if ($apim.provisioningState -ne 'Succeeded' -or $apim.sku.name -ne 'Developer' -or $apim.virtualNetworkType -ne 'External') {
    throw 'APIM must be successfully provisioned in Developer external VNet mode.'
}
Write-Host 'PASS APIM Developer gateway is in external VNet mode'
$apiUrl = "https://management.azure.com/subscriptions/$($state.subscriptionId)/resourceGroups/$($state.resourceGroup)/providers/Microsoft.ApiManagement/service/$($state.outputs.apimName.value)/apis/mcp?api-version=2025-09-01-preview"
$nativeApi = Invoke-Az @('rest', '--method', 'GET', '--url', $apiUrl, '-o', 'json') | ConvertFrom-Json
if ($nativeApi.properties.type -ne 'mcp' -or $nativeApi.properties.backendId -ne 'private-mcp' -or $nativeApi.properties.path -ne '' -or $nativeApi.properties.mcpProperties.endpoints.message.uriTemplate -ne '/mcp') {
    throw 'API must be a native Streamable HTTP MCP registration, not an HTTP-only proxy.'
}
Write-Host 'PASS APIM management plane exposes a native Streamable HTTP MCP server'
$python = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw 'Create the Python virtual environment first; see README.md.' }
for ($i = 0; $i -lt 40; $i++) {
    try {
        $health = Invoke-RestMethod "$($state.outputs.publicUrl.value)/health" -TimeoutSec 15
        if ($health.mode -eq 'azure') { break }
        throw 'Public app must run in azure mode.'
    }
    catch {
        if ($i -eq 39) { throw }
        Write-Host "Waiting for public app startup ($($i + 1)/40): $($_.Exception.Message)"
        Start-Sleep -Seconds 10
    }
}
& $python -c "import httpx,sys; u=sys.argv[1]; r=httpx.get(u+'/mcp'); assert r.status_code==401, r.status_code; print('PASS public MCP rejects anonymous requests'); r=httpx.get(u+'/mcp',headers={'Authorization':'Bearer invalid'}); assert r.status_code==401; print('PASS invalid token rejected')" $state.outputs.publicUrl.value
if ($LASTEXITCODE -ne 0) { throw 'Public authentication check failed.' }
& $python -m app.gateway_checks
if ($LASTEXITCODE -ne 0) { throw 'Gateway OAuth metadata check failed.' }
& $python -c "import httpx,sys; r=httpx.get(sys.argv[1]+'/health',timeout=20); assert r.status_code==403,r.status_code; print('PASS old public App Service endpoint is disabled')" $state.outputs.legacyPublicUrl.value
if ($LASTEXITCODE -ne 0) { throw 'Old public App Service bypass remains accessible.' }
& $python -c "import httpx,sys; r=httpx.get(sys.argv[1]+'/health',timeout=20); assert r.status_code==403,r.status_code; print('PASS private MCP denies public-network access')" $state.outputs.privateUrl.value
if ($LASTEXITCODE -ne 0) { throw 'Private endpoint public-network check failed. Run this from outside the VNet.' }
$token = $null
if (-not $NetworkOnly) {
    $result = Invoke-Az @('account', 'get-access-token', '--scope', "api://$($state.apiAppId)/access_as_user", '-o', 'json') | ConvertFrom-Json
    $token = $result.accessToken
    try {
        $env:MCP_ACCESS_TOKEN = $token
        & $python -m app.client
        if ($LASTEXITCODE -ne 0) { throw 'Authenticated public MCP checks failed.' }
    }
    finally { Remove-Item Env:\MCP_ACCESS_TOKEN -ErrorAction SilentlyContinue }
}
$environment = @(@{ name = 'PRIVATE_URL'; value = $state.outputs.privateUrl.value })
if ($token) { $environment += @{ name = 'MCP_ACCESS_TOKEN'; secureValue = $token } }
try {
    $probeIdentity = "/subscriptions/$($state.subscriptionId)/resourceGroups/$($state.resourceGroup)/providers/Microsoft.ManagedIdentity/userAssignedIdentities/$($state.prefix)-private-id"
    Start-PrivateContainer $state "$($state.prefix)-probe" 'app.probe' $environment $probeIdentity
}
finally {
    Invoke-Az @('container', 'delete', '-g', $state.resourceGroup, '-n', "$($state.prefix)-probe", '--yes', '-o', 'none') | Out-Null
}
if ($token) {
    try {
        $env:MCP_ACCESS_TOKEN = $token
        & $python -m app.gateway_checks --throttle
        if ($LASTEXITCODE -ne 0) { throw 'APIM gateway throttle test failed.' }
    }
    finally {
        $token = $null
        Remove-Item Env:\MCP_ACCESS_TOKEN -ErrorAction SilentlyContinue
    }
}
$authentication = if ($NetworkOnly) { 'not_assessed' } else { 'pass' }
Write-JsonFile @{
    completedAt = [DateTime]::UtcNow.ToString('o')
    topology = 'public-apim-private-mcp-private-postgres'
    imageTag = $state.imageTag
    postgresPrivateNetworking = 'pass'
    postgresEntraOnly = 'pass'
    publicRejectsAnonymous = 'pass'
    publicRejectsInvalidToken = 'pass'
    privateRejectsPublicNetwork = 'pass'
    privateDnsAndConnectivity = 'pass'
    authenticatedPublicTools = $authentication
    authenticatedPrivateTools = $authentication
    readerWriteDenial = $authentication
    apimExternalVnet = 'pass'
    nativeMcpRegistration = 'pass'
    legacyPublicEndpointDisabled = 'pass'
    gatewayOAuthMetadata = 'pass'
    gatewayThrottling = $authentication
} $reportPath
if ($NetworkOnly) {
    Write-Host 'Network checks passed. Authenticated tool access is NOT ASSESSED.'
}
else {
    Write-Host 'Network, authentication, and Reader tool-permission checks passed on both endpoints.'
}
