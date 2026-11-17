targetScope = 'subscription'

metadata description = 'Modular contact-center template with optional web channels, native Dynamics voice, CRM, knowledge and email.'

@minLength(1)
@description('Primary location for all resources.')
param location string = 'swedencentral'

@description('Region for application hosting and new data/network services. AI and shared resources stay in location.')
param hostingLocation string = location
@description('Owner-approved ACS/Email data geography. Confirm service eligibility separately from Azure resource regions.')
param communicationsDataLocation string = 'Europe'
@description('Cosmos DB region. May differ from hosting when subscription region access is restricted.')
param cosmosLocation string = location
@description('Optional Azure AI Search region override. Empty keeps Search in the hosting region.')
param searchLocation string = ''

@description('Resource group to deploy into. Use a separate group for each environment.')
param resourceGroupName string = 'rg-moacc'

@minLength(1)
@description('Name of the azd environment (used for tagging).')
param environmentName string = 'moacc'

@description('Base name for the new Foundry account; a unique suffix is added.')
param foundryAccountName string = 'aif-moacc'

param foundryProjectName string = 'moacc'

@description('Object ID of the deploying user for Foundry and knowledge-base administration.')
param developerPrincipalId string

@description('Model deployment name in the Foundry project.')
param modelDeploymentName string = 'gpt-5.4-mini'
param assistModelDeploymentName string = 'gpt-4.1-mini'
param insightsModelDeploymentName string = 'gpt-5.4-nano'

param deployWebChat bool = true
param deployBrowserVoice bool = false
param deployApplicationPhone bool = false
param deployNativeVoice bool = false
param deployCrm bool = false
param deployEmail bool = false
param deployAgentConsole bool = false
param deployAgentAssist bool = false

@description('Enable retrieval from deployment-approved knowledge sources.')
param enableFoundryIq bool = false

@allowed(['web', 'indexed'])
@description('Use official web plus indexed sources, or indexed sources only.')
param foundryIqMode string = 'indexed'

@description('Deployed Foundry hosted agent name.')
param foundryAgentName string = 'moacc-contact-center'

@description('Container image for the orchestrator API (azd injects the built image; helloworld is the first-provision placeholder).')
param orchestratorImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Container image for the chat UI (azd injects the built image; helloworld is the first-provision placeholder).')
param uiImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Container image for the assist console (azd injects the built image; helloworld is the first-provision placeholder).')
param assistImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Container image for the operator (agent) console (azd injects the built image; helloworld is the first-provision placeholder).')
param agentConsoleImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@secure()
@description('Shared ingestion API key (X-API-Key) for the assist console + channel emitters.')
param ingestApiKey string = ''

@description('Container image for the voice channel (azd injects the built image; helloworld is the first-provision placeholder).')
param voiceImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@secure()
@description('Complete server-side HUMAIN Authorization header. Empty explicitly disables voice until configured.')
param humainAuthorization string = ''

param humainTokenEndpoint string = 'https://livekit-admin.sanad.ink/api/tokens/client'
param humainVoice string = 'mul_nourah'
param humainAsrModel string = 'bayan_playground'
param humainLlmProvider string = 'gemma4'

@description('HUMAIN interruption policy. off uses LiveKit interruption; turn detection is configured separately.')
@allowed([
  'control'
  'shadow'
  'off'
])
param humainInterruptionPolicyMode string = 'off'

@description('Accepted HUMAIN turn detector: fast_vad paired with interruption policy off. Keep both values explicit for an override or rollback.')
@allowed([
  'livekit_v1_mini'
  'fast_vad'
])
param humainTurnTakingStrategy string = 'fast_vad'

@description('ACS source (PSTN) phone number for the voice channel.')
param acsSourceNumber string = ''

@description('Automatically invite the existing fixed conference participant. Disable for isolated Dynamics bot experiments.')
param acsAutoConferenceEnabled bool = false
@description('Explicit application-phone conference target. It must also appear in the private approved dialing policy.')
param acsAutoConferenceNumber string = ''

@description('Dataverse organization URL for native proactive outbound calling. Empty leaves the connector unconfigured.')
param dynamicsOrgUrl string = ''
param dynamicsOutboundConfigId string = ''
@description('Enable new native outbound submissions only after configuring and validating the Dynamics engagement.')
param dynamicsOutboundEnabled bool = false
@secure()
@description('Dedicated server-to-server X-API-Key for native outbound control. Never expose to browser clients.')
param dynamicsOutboundApiKey string = ''
@description('Optional authenticated native call-metadata observer. Leave empty for normal deployments.')
param dynamicsNativeProbeResourceId string = ''
param dynamicsNativeProbeExpiresAt string = ''
@description('Run the native observer without a lease. Requires its resource/key and an empty expiry.')
param dynamicsNativeProbePermanent bool = false
@secure()
param dynamicsNativeProbeApiKey string = ''
@description('Enable the bounded retained-Copilot native-media pilot only after its control topic is prepared.')
param dynamicsNativeMediaEnabled bool = false
@allowed([
  'callautomation'
  'voip'
])
param dynamicsNativeMediaTransport string = 'callautomation'
param dynamicsNativeMediaFastStart bool = false
param dynamicsNativeMediaWorkstreamId string = ''
@description('Optional comma-separated additional native inbound workstream UUIDs. Each remains explicitly validated.')
param dynamicsNativeMediaAdditionalWorkstreamIds string = ''
@description('Opt-in native Pause AI. Requires its Dataverse schema, secured outcomes and published controller pins.')
param dynamicsNativeSupervisionEnabled bool = false
param dynamicsNativeSupervisionActorIds string = ''
param dynamicsNativeSupervisionControllers string = '[]'
@secure()
param dynamicsNativeMediaApiKey string = ''
@secure()
@description('Connection string for the Dynamics ACS resource, not the standalone bot ACS resource.')
param dynamicsNativeMediaAcsConnectionString string = ''

@description('Optional default operator transfer number. Outbound callers can select an operator per call.')
param agentPhoneNumber string = ''

@secure()
@description('ACS connection string (reused from the existing ACS resource).')
param acsConnectionString string = ''

var tags = {
  'azd-env-name': environmentName
  app: 'moacc-chat'
}
var resourceToken = toLower(uniqueString(subscription().id, resourceGroupName, location))

// Reuse (or create) the resource group that already holds the Foundry project.
resource rg 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  scope: rg
  name: 'moacc-chat-resources'
  params: {
    location: location
    hostingLocation: hostingLocation
    communicationsDataLocation: communicationsDataLocation
    cosmosLocation: cosmosLocation
    searchLocation: searchLocation
    resourceToken: resourceToken
    tags: tags
    deployWebChat: deployWebChat
    deployBrowserVoice: deployBrowserVoice
    deployApplicationPhone: deployApplicationPhone
    deployNativeVoice: deployNativeVoice
    deployCrm: deployCrm
    deployEmail: deployEmail
    deployAgentConsole: deployAgentConsole
    deployAgentAssist: deployAgentAssist
    foundryAccountName: foundryAccountName
    foundryProjectName: foundryProjectName
    developerPrincipalId: developerPrincipalId
    modelDeploymentName: modelDeploymentName
    assistModelDeploymentName: assistModelDeploymentName
    insightsModelDeploymentName: insightsModelDeploymentName
    enableFoundryIq: enableFoundryIq
    foundryIqMode: foundryIqMode
    foundryAgentName: foundryAgentName
    orchestratorImage: orchestratorImage
    uiImage: uiImage
    assistImage: assistImage
    agentConsoleImage: agentConsoleImage
    ingestApiKey: ingestApiKey
    voiceImage: voiceImage
    humainAuthorization: humainAuthorization
    humainTokenEndpoint: humainTokenEndpoint
    humainVoice: humainVoice
    humainAsrModel: humainAsrModel
    humainLlmProvider: humainLlmProvider
    humainInterruptionPolicyMode: humainInterruptionPolicyMode
    humainTurnTakingStrategy: humainTurnTakingStrategy
    acsSourceNumber: acsSourceNumber
    acsAutoConferenceEnabled: acsAutoConferenceEnabled
    acsAutoConferenceNumber: acsAutoConferenceNumber
    dynamicsOrgUrl: dynamicsOrgUrl
    dynamicsOutboundConfigId: dynamicsOutboundConfigId
    dynamicsOutboundEnabled: dynamicsOutboundEnabled
    dynamicsOutboundApiKey: dynamicsOutboundApiKey
    dynamicsNativeProbeResourceId: dynamicsNativeProbeResourceId
    dynamicsNativeProbeExpiresAt: dynamicsNativeProbeExpiresAt
    dynamicsNativeProbePermanent: dynamicsNativeProbePermanent
    dynamicsNativeProbeApiKey: dynamicsNativeProbeApiKey
    dynamicsNativeMediaEnabled: dynamicsNativeMediaEnabled
    dynamicsNativeMediaTransport: dynamicsNativeMediaTransport
    dynamicsNativeMediaFastStart: dynamicsNativeMediaFastStart
    dynamicsNativeMediaWorkstreamId: dynamicsNativeMediaWorkstreamId
    dynamicsNativeMediaAdditionalWorkstreamIds: dynamicsNativeMediaAdditionalWorkstreamIds
    dynamicsNativeSupervisionEnabled: dynamicsNativeSupervisionEnabled
    dynamicsNativeSupervisionActorIds: dynamicsNativeSupervisionActorIds
    dynamicsNativeSupervisionControllers: dynamicsNativeSupervisionControllers
    dynamicsNativeMediaApiKey: dynamicsNativeMediaApiKey
    dynamicsNativeMediaAcsConnectionString: dynamicsNativeMediaAcsConnectionString
    agentPhoneNumber: agentPhoneNumber
    acsConnectionString: acsConnectionString
  }
}

output AZURE_LOCATION string = location
output APP_LOCATION string = hostingLocation
output COSMOS_LOCATION string = cosmosLocation
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.acrLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.acrName
output MOACC_UAMI_CLIENT_ID string = resources.outputs.uamiClientId
output MOACC_UAMI_PRINCIPAL_ID string = resources.outputs.uamiPrincipalId
output MOACC_CRM_UAMI_CLIENT_ID string = resources.outputs.crmUamiClientId
output MOACC_CRM_UAMI_PRINCIPAL_ID string = resources.outputs.crmUamiPrincipalId
output MOACC_CRM_UAMI_RESOURCE_ID string = resources.outputs.crmUamiResourceId
output MOACC_KEY_VAULT_NAME string = resources.outputs.keyVaultName
output SERVICE_ORCHESTRATOR_API_URI string = empty(resources.outputs.orchestratorFqdn) ? '' : 'https://${resources.outputs.orchestratorFqdn}'
output SERVICE_UI_V2_URI string = empty(resources.outputs.uiFqdn) ? '' : 'https://${resources.outputs.uiFqdn}'
output SERVICE_AGENT_CONSOLE_URI string = empty(resources.outputs.agentConsoleFqdn) ? '' : 'https://${resources.outputs.agentConsoleFqdn}'
output MOACC_COSMOS_ENDPOINT string = resources.outputs.cosmosEndpoint
output MOACC_COSMOS_DATABASE string = resources.outputs.cosmosDatabaseName
output MOACC_COSMOS_ACCOUNT string = resources.outputs.cosmosAccountName
output MOACC_ACA_ENV_VNET_ID string = resources.outputs.acaEnvVnetResourceId
output MOACC_ACA_ENV_VNET_NAME string = resources.outputs.acaEnvVnetName
output MOACC_SIGNALR_NAME string = resources.outputs.signalRName
output SERVICE_ASSIST_URI string = empty(resources.outputs.assistFqdn) ? '' : 'https://${resources.outputs.assistFqdn}'
output SERVICE_VOICE_URI string = empty(resources.outputs.voiceFqdn) ? '' : 'https://${resources.outputs.voiceFqdn}'
output FOUNDRY_ACCOUNT_NAME string = resources.outputs.foundryAccountName
output FOUNDRY_PROJECT_ENDPOINT string = resources.outputs.foundryProjectEndpoint
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = modelDeploymentName
output AZURE_OPENAI_ENDPOINT string = resources.outputs.openAiEndpoint
output AZURE_COGNITIVE_SERVICES_ENDPOINT string = resources.outputs.cognitiveServicesEndpoint
output FOUNDRY_IQ_SEARCH_ENDPOINT string = resources.outputs.knowledgeSearchEndpoint
output FOUNDRY_IQ_KNOWLEDGE_BASE string = 'organization-knowledge'
output FOUNDRY_IQ_INDEXED_KNOWLEDGE_BASE string = 'organization-indexed'
output FOUNDRY_IQ_KNOWLEDGE_SOURCE string = 'approved-documents'
output FOUNDRY_IQ_WEB_SOURCE string = 'approved-web'
output FOUNDRY_IQ_MODE string = foundryIqMode
output FOUNDRY_IQ_MODEL_DEPLOYMENT string = insightsModelDeploymentName
output FOUNDRY_IQ_API_VERSION string = '2026-04-01'
output FOUNDRY_IQ_MANAGEMENT_API_VERSION string = '2026-08-01-preview'
output MOACC_ACS_NAME string = resources.outputs.acsName
output ACS_EMAIL_SENDER string = resources.outputs.acsEmailSender
