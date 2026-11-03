[CmdletBinding()]
param(
    [string]$Location = 'westeurope',
    [string]$Prefix = '',
    [string]$ImageTag = '',
    [string]$PublisherEmail = '',
    [switch]$ApproveCosts,
    [switch]$SkipBuild
)
. "$PSScriptRoot\common.ps1"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
if (-not $ApproveCosts) {
    throw 'Creating Azure resources incurs charges. Re-run with -ApproveCosts after reading README.md.'
}
$local = Join-Path $root '.deployment'
New-Item -ItemType Directory -Force -Path $local | Out-Null
$statePath = Join-Path $local 'state.json'
if (Test-Path $statePath) {
    $state = Get-Content -Raw $statePath | ConvertFrom-Json
    if ($Prefix -and $Prefix -ne $state.prefix) { throw 'Existing state uses a different prefix.' }
    $Prefix = $state.prefix
    $Location = $state.location
}
else {
    if (-not $Prefix) { $Prefix = 'mcp' + ([guid]::NewGuid().ToString('N').Substring(0, 9)) }
    if ($Prefix -cnotmatch '^[a-z][a-z0-9]{7,17}$') {
        throw 'Prefix must be 8-18 lowercase alphanumeric characters, starting with a letter.'
    }
    $account = Invoke-Az @('account', 'show', '-o', 'json') | ConvertFrom-Json
    $userId = Invoke-Az @('ad', 'signed-in-user', 'show', '--query', 'id', '-o', 'tsv')
    $state = [pscustomobject]@{
        prefix = $Prefix
        subscriptionId = $account.id
        tenantId = $account.tenantId
        demoUserId = $userId
        location = $Location
        resourceGroup = "$Prefix-rg"
        imageTag = 'v1'
        apiAppId = ''
        apiObjectId = ''
        clientAppId = ''
        clientObjectId = ''
        apiSpId = ''
        readerRoleId = [guid]::NewGuid().ToString()
        operatorRoleId = [guid]::NewGuid().ToString()
        scopeId = [guid]::NewGuid().ToString()
        outputs = $null
        localDirectory = $local
    }
    Write-JsonFile $state $statePath
}
if ($ImageTag) {
    if ($ImageTag -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$') {
        throw 'ImageTag contains invalid characters.'
    }
    $state.imageTag = $ImageTag
    Write-JsonFile $state $statePath
}
$account = Invoke-Az @('account', 'show', '-o', 'json') | ConvertFrom-Json
if ($account.id -ne $state.subscriptionId -or $account.tenantId -ne $state.tenantId) {
    throw 'Azure CLI context differs from the saved deployment. Switch explicitly before continuing.'
}
Write-Host "Deploying $Prefix to subscription $($state.subscriptionId), tenant $($state.tenantId)"
if (-not $PublisherEmail) {
    $PublisherEmail = Invoke-Az @('ad', 'signed-in-user', 'show', '--query', 'mail', '-o', 'tsv')
}
if ($PublisherEmail -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') {
    throw 'Supply -PublisherEmail with a valid email address for APIM provisioning.'
}

if (-not $state.apiAppId) {
    $app = Invoke-Az @('ad', 'app', 'create', '--display-name', "$Prefix MCP API", '--sign-in-audience', 'AzureADMyOrg', '-o', 'json') | ConvertFrom-Json
    $state.apiAppId = $app.appId
    $state.apiObjectId = $app.id
    Write-JsonFile $state $statePath
}
if (-not $state.clientAppId) {
    $client = Invoke-Az @('ad', 'app', 'create', '--display-name', "$Prefix Reader CLI", '--sign-in-audience', 'AzureADMyOrg', '--is-fallback-public-client', 'true', '-o', 'json') | ConvertFrom-Json
    $state.clientAppId = $client.appId
    $state.clientObjectId = $client.id
    Write-JsonFile $state $statePath
}
$clientSp = Invoke-Az @('ad', 'sp', 'list', '--filter', "appId eq '$($state.clientAppId)'", '-o', 'json') | ConvertFrom-Json
if (-not $clientSp) {
    Invoke-Az @('ad', 'sp', 'create', '--id', $state.clientAppId, '-o', 'none') | Out-Null
}
$manifest = @{
    identifierUris = @("api://$($state.apiAppId)")
    api = @{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes = @(@{
            id = $state.scopeId; value = 'access_as_user'; type = 'User'; isEnabled = $true
            adminConsentDisplayName = 'Access the synthetic MCP demo'
            adminConsentDescription = 'Invoke demo tools as the signed-in user.'
            userConsentDisplayName = 'Access the synthetic MCP demo'
            userConsentDescription = 'Invoke demo tools as you.'
        })
    }
    appRoles = @(
        @{ id = $state.readerRoleId; allowedMemberTypes = @('User'); displayName = 'Reader'; description = 'Read your synthetic profile and requests'; isEnabled = $true; value = 'Reader' },
        @{ id = $state.operatorRoleId; allowedMemberTypes = @('User'); displayName = 'Operator'; description = 'Create synthetic requests as well as read'; isEnabled = $true; value = 'Operator' }
    )
}
$manifestPath = Join-Path $local 'api-manifest.json'
Write-JsonFile $manifest $manifestPath
Invoke-Az @('rest', '--method', 'PATCH', '--url', "https://graph.microsoft.com/v1.0/applications/$($state.apiObjectId)", '--body', "@$manifestPath", '-o', 'none') | Out-Null
$clientManifestPath = Join-Path $local 'client-manifest.json'
Write-JsonFile @{
    requiredResourceAccess = @(@{
        resourceAppId = $state.apiAppId
        resourceAccess = @(@{ id = $state.scopeId; type = 'Scope' })
    })
} $clientManifestPath
Invoke-Az @('rest', '--method', 'PATCH', '--url', "https://graph.microsoft.com/v1.0/applications/$($state.clientObjectId)", '--body', "@$clientManifestPath", '-o', 'none') | Out-Null
$preauthPath = Join-Path $local 'preauthorization.json'
Write-JsonFile @{
    api = @{
        preAuthorizedApplications = @(
            @{ appId = $state.clientAppId; delegatedPermissionIds = @($state.scopeId) },
            @{ appId = '04b07795-8ddb-461a-bbee-02f9e1bf7b46'; delegatedPermissionIds = @($state.scopeId) }
        )
    }
} $preauthPath
Invoke-Az @('rest', '--method', 'PATCH', '--url', "https://graph.microsoft.com/v1.0/applications/$($state.apiObjectId)", '--body', "@$preauthPath", '-o', 'none') | Out-Null
if (-not $state.apiSpId) {
    $sp = Invoke-Az @('ad', 'sp', 'create', '--id', $state.apiAppId, '-o', 'json') | ConvertFrom-Json
    $state.apiSpId = $sp.id
    Write-JsonFile $state $statePath
}
$spPatch = Join-Path $local 'sp-policy.json'
Write-JsonFile @{ appRoleAssignmentRequired = $true } $spPatch
Invoke-Az @('rest', '--method', 'PATCH', '--url', "https://graph.microsoft.com/v1.0/servicePrincipals/$($state.apiSpId)", '--body', "@$spPatch", '-o', 'none') | Out-Null
$assignments = Invoke-Az @('rest', '--method', 'GET', '--url', "https://graph.microsoft.com/v1.0/users/$($state.demoUserId)/appRoleAssignments", '-o', 'json') | ConvertFrom-Json
if (-not ($assignments.value | Where-Object { $_.resourceId -eq $state.apiSpId -and $_.appRoleId -eq $state.readerRoleId })) {
    $assignmentFile = Join-Path $local 'reader-assignment.json'
    Write-JsonFile @{ principalId = $state.demoUserId; resourceId = $state.apiSpId; appRoleId = $state.readerRoleId } $assignmentFile
    Invoke-Az @('rest', '--method', 'POST', '--url', "https://graph.microsoft.com/v1.0/users/$($state.demoUserId)/appRoleAssignments", '--body', "@$assignmentFile", '-o', 'none') | Out-Null
}

foreach ($provider in @('Microsoft.Web', 'Microsoft.Network', 'Microsoft.ManagedIdentity', 'Microsoft.ContainerRegistry', 'Microsoft.ContainerInstance', 'Microsoft.DBforPostgreSQL', 'Microsoft.ApiManagement')) {
    Invoke-Az @('provider', 'register', '--namespace', $provider, '--wait', '-o', 'none') | Out-Null
}
Invoke-Az @('group', 'create', '-n', $state.resourceGroup, '-l', $Location, '--tags', 'purpose=mcp-security-demo', 'data=synthetic', '-o', 'none') | Out-Null
$servers = Invoke-Az @('postgres', 'flexible-server', 'list', '-g', $state.resourceGroup, '-o', 'json') | ConvertFrom-Json
$existingDatabase = $servers | Where-Object { $_.name -eq "$Prefix-pg" }
if ($existingDatabase.state -eq 'Stopped') {
    Write-Host 'Starting the existing demo PostgreSQL server before applying configuration.'
    Invoke-Az @('postgres', 'flexible-server', 'start', '-g', $state.resourceGroup, '-n', "$Prefix-pg", '-o', 'none') | Out-Null
}
# Build before switching existing apps to the new tag to avoid pulling a missing image.
$built = $false
if (-not $SkipBuild -and $state.outputs.registryName.value) {
    Invoke-Az @('acr', 'build', '-r', $state.outputs.registryName.value, '-t', "mcpdemo:$($state.imageTag)", '.', '--no-logs', '-o', 'none') | Out-Null
    $built = $true
}
Remove-LegacyMcpOperations $state
$deployment = Invoke-Az @(
    'deployment', 'group', 'create', '-g', $state.resourceGroup, '-n', 'mcp-sample',
    '--template-file', "$root\infra\main.bicep",
    '--parameters', "prefix=$Prefix", "location=$Location", "tenantId=$($state.tenantId)",
    "apiClientId=$($state.apiAppId)", "imageTag=$($state.imageTag)", "publisherEmail=$PublisherEmail", '-o', 'json'
) | ConvertFrom-Json
$state.outputs = $deployment.properties.outputs
Write-JsonFile $state $statePath
if (-not $SkipBuild -and -not $built) {
    Invoke-Az @('acr', 'build', '-r', $state.outputs.registryName.value, '-t', "mcpdemo:$($state.imageTag)", '.', '--no-logs', '-o', 'none') | Out-Null
}
foreach ($suffix in @('public', 'private')) {
    Invoke-Az @('webapp', 'restart', '-g', $state.resourceGroup, '-n', "$Prefix-$suffix") | Out-Null
}
$principals = @(
    @{ name = $state.outputs.publicIdentityName.value; oid = $state.outputs.publicIdentityOid.value },
    @{ name = $state.outputs.privateIdentityName.value; oid = $state.outputs.privateIdentityOid.value }
) | ConvertTo-Json -Compress
$envs = @(
    @{ name = 'PGHOST'; value = $state.outputs.postgresHost.value },
    @{ name = 'PGDATABASE'; value = 'postgres' },
    @{ name = 'PGUSER'; value = $state.outputs.bootstrapName.value },
    @{ name = 'AZURE_CLIENT_ID'; value = $state.outputs.bootstrapClientId.value },
    @{ name = 'TENANT_ID'; value = $state.tenantId },
    @{ name = 'DEMO_USER_OID'; value = $state.demoUserId },
    @{ name = 'RUNTIME_PRINCIPALS'; value = $principals }
)
Start-PrivateContainer $state "$Prefix-init" 'app.bootstrap' $envs $state.outputs.bootstrapIdentityId.value
Invoke-Az @('container', 'delete', '-g', $state.resourceGroup, '-n', "$Prefix-init", '--yes', '-o', 'none') | Out-Null
Write-Host "Deployment complete. Run .\scripts\verify.ps1 for functional and network checks."
Write-Host "Public MCP: $($state.outputs.publicUrl.value)/mcp"
Write-Host "Private MCP: $($state.outputs.privateUrl.value)/mcp"
