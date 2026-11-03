[CmdletBinding()]
param([switch]$ConfirmDelete)
. "$PSScriptRoot\common.ps1"
$root = Split-Path $PSScriptRoot -Parent
$path = "$root\.deployment\state.json"
$state = Get-Content -Raw $path | ConvertFrom-Json
$account = Invoke-Az @('account', 'show', '-o', 'json') | ConvertFrom-Json
if ($account.id -ne $state.subscriptionId -or $account.tenantId -ne $state.tenantId) {
    throw 'Azure CLI context differs from saved deployment.'
}
if (-not $ConfirmDelete) {
    throw "Re-run with -ConfirmDelete to delete resource group $($state.resourceGroup) and the two sample app registrations."
}
Invoke-Az @('group', 'delete', '-n', $state.resourceGroup, '--yes', '-o', 'none') | Out-Null
foreach ($id in @($state.clientObjectId, $state.apiObjectId)) {
    if ($id) { Invoke-Az @('ad', 'app', 'delete', '--id', $id) | Out-Null }
}
Remove-Item -LiteralPath $path
Write-Host 'Sample resource group and Entra app registrations deleted.'
