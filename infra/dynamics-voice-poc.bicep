@description('Dedicated ACS resource for the Dynamics environment, separate from the application phone number.')
param acsName string = 'acs-moacc-dynamics-${uniqueString(subscription().id, resourceGroup().id)}'

@description('ACS data geography. Match the Dynamics deployment requirements; this is not the phone-number country.')
param dataLocation string

resource communicationService 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: acsName
  location: 'global'
  tags: {
    app: 'moacc'
    purpose: 'dynamics-voice-poc'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    dataLocation: dataLocation
  }
}

output acsName string = communicationService.name
output acsResourceId string = communicationService.id
output acsEndpoint string = 'https://${communicationService.properties.hostName}'
output acsImmutableResourceId string = communicationService.properties.immutableResourceId
