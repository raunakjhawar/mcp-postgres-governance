targetScope = 'resourceGroup'

@description('Globally unique lowercase alphanumeric prefix, 8-18 characters.')
@minLength(8)
@maxLength(18)
param prefix string
param location string = resourceGroup().location
param tenantId string
param apiClientId string
param imageTag string
param publisherEmail string
param publisherName string = 'MCP Security Demo'

var pgName = '${prefix}-pg'
var publicName = '${prefix}-public'
var privateName = '${prefix}-private'
var gatewayUrl = 'https://${prefix}-apim.azure-api.net'
var acrPullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource apimNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${prefix}-apim-nsg'
  location: location
  properties: {
    securityRules: [
      {
        name: 'GatewayHttps'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'ApimManagement'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'ApiManagement'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '3443'
        }
      }
      {
        name: 'LoadBalancer'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '6390'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${prefix}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.74.0.0/16'] }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: '10.74.0.0/24'
          delegations: [
            { name: 'web', properties: { serviceName: 'Microsoft.Web/serverFarms' } }
          ]
        }
      }
      {
        name: 'postgres'
        properties: {
          addressPrefix: '10.74.1.0/24'
          delegations: [
            { name: 'postgres', properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' } }
          ]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: '10.74.2.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'probes'
        properties: {
          addressPrefix: '10.74.3.0/24'
          delegations: [
            { name: 'aci', properties: { serviceName: 'Microsoft.ContainerInstance/containerGroups' } }
          ]
        }
      }
      {
        name: 'apim'
        properties: {
          addressPrefix: '10.74.4.0/24'
          networkSecurityGroup: { id: apimNsg.id }
        }
      }
    ]
  }
}

resource pgDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: '${prefix}.postgres.database.azure.com'
  location: 'global'
}
resource pgDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: pgDns
  name: '${prefix}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}
resource appDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.azurewebsites.net'
  location: 'global'
}
resource appDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: appDns
  name: '${prefix}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

resource bootstrapIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-bootstrap'
  location: location
}
resource publicIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-public-id'
  location: location
}
resource privateIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-private-id'
  location: location
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${prefix}acr'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}
resource bootstrapPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, bootstrapIdentity.id, acrPullRole)
  scope: registry
  properties: {
    principalId: bootstrapIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRole
  }
}
resource publicPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, publicIdentity.id, acrPullRole)
  scope: registry
  properties: {
    principalId: publicIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRole
  }
}
resource privatePull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, privateIdentity.id, acrPullRole)
  scope: registry
  properties: {
    principalId: privateIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRole
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: pgName
  location: location
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenantId
    }
    storage: { storageSizeGB: 32, autoGrow: 'Enabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: {
      delegatedSubnetResourceId: '${vnet.id}/subnets/postgres'
      privateDnsZoneArmResourceId: pgDns.id
      publicNetworkAccess: 'Disabled'
    }
  }
  dependsOn: [pgDnsLink]
}
module pgAdmin 'postgres-admin.bicep' = {
  name: '${prefix}-pg-admin'
  params: {
    postgresName: postgres.name
    principalId: bootstrapIdentity.properties.principalId
    principalName: bootstrapIdentity.name
    tenantId: tenantId
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${prefix}-plan'
  location: location
  kind: 'linux'
  sku: { name: 'B1', tier: 'Basic', capacity: 1 }
  properties: { reserved: true }
}

var identityIds = [publicIdentity.id, privateIdentity.id]
var clientIds = [publicIdentity.properties.clientId, privateIdentity.properties.clientId]
var dbUsers = [publicIdentity.name, privateIdentity.name]

resource apps 'Microsoft.Web/sites@2024-04-01' = [for (name, i) in [publicName, privateName]: {
  name: name
  location: location
  kind: 'app,linux,container'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityIds[i]}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: 'Disabled'
    virtualNetworkSubnetId: '${vnet.id}/subnets/apps'
    siteConfig: {
      linuxFxVersion: 'DOCKER|${registry.properties.loginServer}/mcpdemo:${imageTag}'
      acrUseManagedIdentityCreds: true
      acrUserManagedIdentityID: clientIds[i]
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      healthCheckPath: '/health'
      appSettings: [
        { name: 'APP_MODE', value: 'azure' }
        { name: 'TENANT_ID', value: tenantId }
        { name: 'API_CLIENT_ID', value: apiClientId }
        { name: 'PUBLIC_URL', value: 'https://${name}.azurewebsites.net' }
        { name: 'GATEWAY_URL', value: gatewayUrl }
        { name: 'PGHOST', value: postgres.properties.fullyQualifiedDomainName }
        { name: 'PGDATABASE', value: 'postgres' }
        { name: 'PGUSER', value: dbUsers[i] }
        { name: 'AZURE_CLIENT_ID', value: clientIds[i] }
        { name: 'WEBSITES_PORT', value: '8000' }
        { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'false' }
        { name: 'REQUESTS_PER_MINUTE', value: '60' }
      ]
    }
  }
  dependsOn: [publicPull, privatePull]
}]

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${prefix}-mcp-pe'
  location: location
  properties: {
    subnet: { id: '${vnet.id}/subnets/private-endpoints' }
    privateLinkServiceConnections: [
      {
        name: 'mcp'
        properties: {
          privateLinkServiceId: apps[1].id
          groupIds: ['sites']
        }
      }
    ]
  }
}
resource privateZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'app', properties: { privateDnsZoneId: appDns.id } }
    ]
  }
}

module gateway 'apim.bicep' = {
  name: '${prefix}-gateway'
  params: {
    name: '${prefix}-apim'
    location: location
    subnetId: '${vnet.id}/subnets/apim'
    publisherEmail: publisherEmail
    publisherName: publisherName
    tenantId: tenantId
    apiClientId: apiClientId
    backendUrl: 'https://${privateName}.azurewebsites.net'
  }
  dependsOn: [privateZoneGroup, appDnsLink]
}

output publicUrl string = gatewayUrl
output legacyPublicUrl string = 'https://${publicName}.azurewebsites.net'
output apimName string = '${prefix}-apim'
output privateUrl string = 'https://${privateName}.azurewebsites.net'
output registryName string = registry.name
output registryServer string = registry.properties.loginServer
output postgresHost string = postgres.properties.fullyQualifiedDomainName
output postgresName string = postgres.name
output bootstrapIdentityId string = bootstrapIdentity.id
output bootstrapClientId string = bootstrapIdentity.properties.clientId
output bootstrapName string = bootstrapIdentity.name
output probeSubnetId string = '${vnet.id}/subnets/probes'
output publicIdentityName string = publicIdentity.name
output publicIdentityOid string = publicIdentity.properties.principalId
output privateIdentityName string = privateIdentity.name
output privateIdentityOid string = privateIdentity.properties.principalId
