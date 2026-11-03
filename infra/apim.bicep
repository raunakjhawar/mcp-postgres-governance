param name string
param location string
param subnetId string
param publisherEmail string
param publisherName string
param tenantId string
param apiClientId string
param backendUrl string

var gatewayUrl = 'https://${name}.azure-api.net'
var policy = replace(replace(replace(replace(
  loadTextContent('apim-policy.xml'),
  '__TENANT_ID__', tenantId),
  '__API_CLIENT_ID__', apiClientId),
  '__GATEWAY_URL__', gatewayUrl),
  '__BACKEND_URL__', backendUrl)

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: name
  location: location
  sku: { name: 'Developer', capacity: 1 }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
    virtualNetworkType: 'External'
    virtualNetworkConfiguration: { subnetResourceId: subnetId }
    customProperties: {
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls10': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls11': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls10': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls11': 'False'
    }
  }
}
resource mcpBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  parent: apim
  name: 'private-mcp'
  properties: {
    protocol: 'http'
    url: backendUrl
    type: 'Single'
    tls: {
      validateCertificateChain: true
      validateCertificateName: true
    }
  }
}
resource api 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = {
  parent: apim
  name: 'mcp'
  properties: {
    displayName: 'Private PostgreSQL MCP'
    description: 'Native existing MCP server registration backed by private Azure PostgreSQL.'
    path: ''
    protocols: ['https']
    subscriptionRequired: false
    backendId: mcpBackend.name
    serviceUrl: backendUrl
    type: 'mcp'
    mcpProperties: {
      transportType: 'streamable'
      // The service accepts a named endpoint map, despite the preview array schema.
      #disable-next-line BCP037
      endpoints: {
        message: { uriTemplate: '/mcp' }
      }
    }
  }
}
resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: { format: 'rawxml', value: policy }
}

resource supportApis 'Microsoft.ApiManagement/service/apis@2024-05-01' = [for item in [
  { name: 'mcp-health', path: 'health', operation: 'health', template: '/' }
  { name: 'mcp-discovery', path: '.well-known', operation: 'metadata', template: '/oauth-protected-resource' }
]: {
  parent: apim
  name: item.name
  properties: {
    displayName: item.name
    path: item.path
    protocols: ['https']
    subscriptionRequired: false
    serviceUrl: backendUrl
    type: 'http'
  }
}]
resource supportOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for (item, i) in [
  { name: 'health', template: '/' }
  { name: 'metadata', template: '/oauth-protected-resource' }
]: {
  parent: supportApis[i]
  name: item.name
  properties: {
    displayName: item.name
    method: 'GET'
    urlTemplate: item.template
    templateParameters: []
    responses: []
  }
}]
resource supportPolicies 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = [for i in range(0, 2): {
  parent: supportApis[i]
  name: 'policy'
  properties: { format: 'rawxml', value: policy }
  dependsOn: [supportOperations]
}]
