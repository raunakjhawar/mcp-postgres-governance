[CmdletBinding()]
param()
. "$PSScriptRoot\common.ps1"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$state = Get-Content -Raw "$root\.deployment\state.json" | ConvertFrom-Json
$account = Invoke-Az @('account', 'show', '-o', 'json') | ConvertFrom-Json
if ($account.id -ne $state.subscriptionId -or $account.tenantId -ne $state.tenantId) {
    throw 'Azure CLI context differs from saved deployment.'
}
if (-not $state.outputs.apimName.value) {
    throw 'Deploy the base environment before updating the gateway.'
}
$apim = Invoke-Az @('apim', 'show', '-g', $state.resourceGroup, '-n', $state.outputs.apimName.value, '-o', 'json') | ConvertFrom-Json
$reportPath = "$root\.deployment\verification.json"
if (Test-Path -LiteralPath $reportPath) { Remove-Item -LiteralPath $reportPath }
Remove-LegacyMcpOperations $state
Invoke-Az @(
    'deployment', 'group', 'create', '-g', $state.resourceGroup, '-n', 'mcp-native-registration',
    '--template-file', "$root\infra\apim.bicep",
    '--parameters', "name=$($apim.name)", "location=$($apim.location)",
    "subnetId=$($apim.virtualNetworkConfiguration.subnetResourceId)",
    "publisherEmail=$($apim.publisherEmail)", "publisherName=$($apim.publisherName)",
    "tenantId=$($state.tenantId)", "apiClientId=$($state.apiAppId)",
    "backendUrl=$($state.outputs.privateUrl.value)", '-o', 'none'
) | Out-Null
Write-Host 'Native MCP gateway registration deployed. Run scripts\verify.ps1 before declaring it ready.'
