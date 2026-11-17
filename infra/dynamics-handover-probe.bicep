@description('Existing Event Grid system topic for the Dynamics ACS resource.')
param systemTopicName string = 'eg-moacc-dynamics-voice'

@description('HTTPS endpoint on the existing voice service, ending in /dynamics/handover/probe/events.')
@secure()
param webhookUrl string

@description('Event subscription expiry in UTC. Use an empty string only with permanent mode.')
param expiresAt string

@description('Omit the Event Grid subscription expiry for a continuously operating native observer.')
param permanent bool = false

@secure()
@minLength(32)
param apiKey string

resource systemTopic 'Microsoft.EventGrid/systemTopics@2022-06-15' existing = {
  name: systemTopicName
}

resource observer 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2022-06-15' = {
  parent: systemTopic
  name: 'moacc-native-handover-probe'
  properties: {
    destination: {
      endpointType: 'WebHook'
      properties: {
        endpointUrl: webhookUrl
        maxEventsPerBatch: 1
        deliveryAttributeMappings: [
          {
            name: 'X-API-Key'
            type: 'Static'
            properties: {
              value: apiKey
              isSecret: true
            }
          }
        ]
      }
    }
    filter: {
      includedEventTypes: [
        'Microsoft.Communication.IncomingCall'
      ]
    }
    eventDeliverySchema: 'EventGridSchema'
    ...(permanent ? {} : { expirationTimeUtc: expiresAt })
    retryPolicy: {
      maxDeliveryAttempts: 3
      eventTimeToLiveInMinutes: 5
    }
  }
}

output eventSubscriptionId string = observer.id
