metadata description = 'Shared platform and selected contact-center services, identities, models and communications resources.'

@description('Primary location for all resources.')
param location string
param hostingLocation string = location
param communicationsDataLocation string = 'Europe'
param cosmosLocation string = location
param searchLocation string = ''

@description('Short, unique token used to name resources.')
param resourceToken string

@description('Tags applied to every resource.')
param tags object

@description('Container image for the orchestrator API. Overridden by azd at deploy time.')
param orchestratorImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Container image for the chat UI. Overridden by azd at deploy time.')
param uiImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Container image for the assist console. Overridden by azd at deploy time.')
param assistImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Container image for the operator (agent) console. Overridden by azd at deploy time.')
param agentConsoleImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@secure()
@description('Shared ingestion API key (X-API-Key) for the assist console + channel emitters.')
param ingestApiKey string = ''

param foundryAccountName string
param foundryProjectName string
param developerPrincipalId string

@description('Model deployment name in the Foundry project.')
param modelDeploymentName string
param assistModelDeploymentName string
param insightsModelDeploymentName string
param enableFoundryIq bool = true
param foundryIqMode string = 'web'

@description('Deployed Foundry hosted agent name.')
param foundryAgentName string

@description('Container image for the voice channel. Overridden by azd at deploy time.')
param voiceImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@secure()
param humainAuthorization string = ''
param humainTokenEndpoint string = 'https://livekit-admin.sanad.ink/api/tokens/client'
param humainVoice string = 'mul_nourah'
param humainAsrModel string = 'bayan_playground'
param humainLlmProvider string = 'gemma4'
param deployWebChat bool = true
param deployBrowserVoice bool = false
param deployApplicationPhone bool = false
param deployNativeVoice bool = false
param deployCrm bool = false
param deployEmail bool = false
param deployAgentConsole bool = false
param deployAgentAssist bool = false
@allowed([
  'control'
  'shadow'
  'off'
])
param humainInterruptionPolicyMode string = 'off'
@allowed([
  'livekit_v1_mini'
  'fast_vad'
])
param humainTurnTakingStrategy string = 'fast_vad'

@description('ACS source (PSTN) phone number for the voice channel.')
param acsSourceNumber string = ''
param acsAutoConferenceEnabled bool = false
param acsAutoConferenceNumber string = ''
param agentPhoneNumber string = ''
param dynamicsOrgUrl string = ''
param dynamicsOutboundConfigId string = ''
param dynamicsOutboundEnabled bool = false
@secure()
param dynamicsOutboundApiKey string = ''
param dynamicsNativeProbeResourceId string = ''
param dynamicsNativeProbeExpiresAt string = ''
param dynamicsNativeProbePermanent bool = false
@secure()
param dynamicsNativeProbeApiKey string = ''
param dynamicsNativeMediaEnabled bool = false
param dynamicsNativeMediaTransport string = 'callautomation'
param dynamicsNativeMediaFastStart bool = false
param dynamicsNativeMediaWorkstreamId string = ''
param dynamicsNativeMediaAdditionalWorkstreamIds string = ''
param dynamicsNativeSupervisionEnabled bool = false
param dynamicsNativeSupervisionActorIds string = ''
param dynamicsNativeSupervisionControllers string = '[]'
@secure()
param dynamicsNativeMediaApiKey string = ''
@secure()
param dynamicsNativeMediaAcsConnectionString string = ''

@secure()
@description('ACS connection string (reused from the existing ACS resource).')
param acsConnectionString string = ''

var abbrs = {
  registry: 'crmoacc'
  keyVault: 'kv-moacc-'
  identity: 'id-moacc-'
  logAnalytics: 'log-moacc-'
  appInsights: 'appi-moacc-'
  acaEnv: 'cae-moacc-'
}
var appEnvironmentName = '${abbrs.acaEnv}${resourceToken}-${hostingLocation}'

// ---------------- Observability (shared) ----------------
module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.11.2' = {
  name: 'logAnalytics'
  params: {
    name: '${abbrs.logAnalytics}${resourceToken}'
    location: location
    tags: tags
    dataRetention: 30
    skuName: 'PerGB2018'
  }
}

module appInsights 'br/public:avm/res/insights/component:0.6.0' = {
  name: 'appInsights'
  params: {
    name: '${abbrs.appInsights}${resourceToken}'
    location: location
    tags: tags
    workspaceResourceId: logAnalytics.outputs.resourceId
    applicationType: 'web'
  }
}

// ---------------- Identity (shared) ----------------
module uami 'br/public:avm/res/managed-identity/user-assigned-identity:0.4.1' = {
  name: 'uami'
  params: {
    name: '${abbrs.identity}${resourceToken}'
    location: location
    tags: tags
  }
}

var deployVoice = deployBrowserVoice || deployApplicationPhone || deployNativeVoice
var deployUi = deployWebChat || deployBrowserVoice || deployApplicationPhone
var deployServiceApi = deployCrm || enableFoundryIq || deployAgentAssist
var deployFoundry = deployWebChat || enableFoundryIq || deployAgentAssist
var deployCommunications = deployEmail || deployApplicationPhone

module foundry 'foundry/foundry.bicep' = if (deployFoundry) {
  name: 'foundry'
  params: {
    location: location
    foundryAccountName: foundryAccountName
    projectName: foundryProjectName
    developerPrincipalId: developerPrincipalId
    developerPrincipalType: 'User'
    runtimePrincipalId: uami.outputs.principalId
    modelCapacity: 50
    modelDeploymentName: modelDeploymentName
    assistModelDeploymentName: assistModelDeploymentName
    insightsModelDeploymentName: insightsModelDeploymentName
    deployChatModel: deployWebChat
    deployAssistModel: deployAgentAssist
    deployInsightsModel: deployAgentAssist || enableFoundryIq
    tags: tags
  }
}

module knowledge 'knowledge.bicep' = if (enableFoundryIq) {
  name: 'knowledge'
  params: {
    location: empty(searchLocation) ? hostingLocation : searchLocation
    resourceToken: resourceToken
    tags: tags
    runtimePrincipalId: uami.outputs.principalId
    developerPrincipalId: developerPrincipalId
    projectPrincipalId: foundry!.outputs.projectPrincipalId
    foundryAccountName: foundry!.outputs.accountName
  }
}

// ---------------- Container Registry (shared) ----------------
module registry 'br/public:avm/res/container-registry/registry:0.9.1' = {
  name: 'registry'
  params: {
    name: '${abbrs.registry}${resourceToken}'
    location: location
    tags: tags
    acrSku: 'Standard'
    acrAdminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    // UAMI pulls images.
    roleAssignments: [
      {
        principalId: uami.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'AcrPull'
      }
    ]
  }
}

// ---------------- Key Vault (shared) ----------------
module keyVault 'br/public:avm/res/key-vault/vault:0.12.1' = {
  name: 'keyVault'
  params: {
    name: '${abbrs.keyVault}${resourceToken}'
    location: location
    tags: tags
    sku: 'standard'
    enableRbacAuthorization: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 7
    roleAssignments: [
      {
        principalId: uami.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Key Vault Secrets User'
      }
    ]
  }
}

// ---------------- Container Apps Environment (shared) ----------------
module acaEnv 'br/public:avm/res/app/managed-environment:0.11.1' = {
  name: 'acaEnv'
  params: {
    name: appEnvironmentName
    location: hostingLocation
    tags: tags
    zoneRedundant: false
    publicNetworkAccess: 'Enabled'
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appInsightsConnectionString: appInsights.outputs.connectionString
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
  }
}

resource monitoredEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: appEnvironmentName
}

resource runtimeDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'runtime-logs'
  scope: monitoredEnvironment
  properties: {
    workspaceId: logAnalytics.outputs.resourceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      { category: 'ContainerAppConsoleLogs', enabled: true }
      { category: 'ContainerAppSystemLogs', enabled: true }
    ]
  }
  dependsOn: [acaEnv]
}

var registryServer = '${registry.outputs.name}.azurecr.io'

// ---------------- Email (Azure Communication Services Email, ECS) ----------------
// Email service + an auto-verified Azure Managed Domain (DoNotReply@<guid>.azurecomm.net), linked
// to a dedicated ACS resource so chat + voice can send transactional email via its connection string.
resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = if (deployEmail) {
  name: 'email-moacc-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    dataLocation: communicationsDataLocation
  }
}

resource emailDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = if (deployEmail) {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource acsEmail 'Microsoft.Communication/communicationServices@2023-04-01' = if (deployCommunications) {
  name: 'acs-moacc-${resourceToken}'
  location: 'global'
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    dataLocation: communicationsDataLocation
    linkedDomains: deployEmail ? [emailDomain!.id] : []
  }
}

resource emailDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deployEmail) {
  scope: acsEmail
  name: 'email-delivery'
  properties: {
    workspaceId: logAnalytics.outputs.resourceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      { category: 'EmailSendMailOperational', enabled: true }
      { category: 'EmailStatusUpdateOperational', enabled: true }
    ]
  }
}

resource callDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deployApplicationPhone) {
  scope: acsEmail
  name: 'call-diagnostics'
  properties: {
    workspaceId: logAnalytics.outputs.resourceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      { category: 'CallSummary', enabled: true }
      { category: 'CallAutomationOperational', enabled: true }
      { category: 'CallAutomationMediaSummary', enabled: true }
    ]
  }
}

var acsEmailSender = deployEmail ? 'DoNotReply@${emailDomain!.properties.fromSenderDomain}' : ''
var emailConnectionString = deployEmail ? acsEmail!.listKeys().primaryConnectionString : ''
var voiceAcsConnectionString = deployApplicationPhone ? (empty(acsConnectionString) ? acsEmail!.listKeys().primaryConnectionString : acsConnectionString) : ''
var foundryOpenAiEndpoint = deployFoundry ? foundry!.outputs.openAiEndpoint : ''
var foundryProjectEndpoint = deployFoundry ? foundry!.outputs.projectEndpoint : ''
var foundrySpeechEndpoint = deployFoundry ? foundry!.outputs.cognitiveServicesEndpoint : ''
var assistOrigin = deployServiceApi ? 'https://${dataplane!.outputs.assistFqdn}' : ''
var orchestratorOrigin = deployWebChat ? 'https://${orchestratorApp!.outputs.fqdn}' : ''
var uiOrigin = deployUi ? 'https://${uiApp!.outputs.fqdn}' : ''

// Extension-resource scopes must be known before deployment; mirror the module's naming formula.
var foundryAccountResourceName = '${foundryAccountName}-${toLower(uniqueString(resourceGroup().id, foundryAccountName))}'
resource speechAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountResourceName
}

resource acsSpeechAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployApplicationPhone && deployAgentAssist) {
  scope: speechAccount
  name: guid(speechAccount.id, acsEmail.id, 'cognitive-services-user')
  properties: {
    principalId: acsEmail!.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
  }
  dependsOn: [foundry]
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: speechAccount
  name: foundryProjectName
}

resource knowledgeConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = if (enableFoundryIq) {
  parent: foundryProject
  name: 'organization-knowledge-search'
  properties: {
    authType: 'AAD'
    category: 'CognitiveSearch'
    target: knowledge!.outputs.endpoint
    isSharedToAll: false
    useWorkspaceManagedIdentity: true
    metadata: {
      ApiType: 'Azure'
      ResourceId: knowledge!.outputs.resourceId
    }
  }
  dependsOn: [foundry]
}

// ---------------- Unified data backbone (Cosmos + VNet + private endpoint + SignalR + VNet ACA env) ----------------
module dataplane 'dataplane.bicep' = if (deployServiceApi) {
  name: 'dataplane'
  params: {
    location: hostingLocation
    cosmosLocation: cosmosLocation
    resourceToken: resourceToken
    tags: tags
    deployAgentAssist: deployAgentAssist
    deployCrm: deployCrm
    publicEnvironmentResourceId: acaEnv.outputs.resourceId
    uamiResourceId: uami.outputs.resourceId
    uamiPrincipalId: uami.outputs.principalId
    logAnalyticsCustomerId: logAnalytics.outputs.logAnalyticsWorkspaceId
    logAnalyticsSharedKey: logAnalytics.outputs.primarySharedKey
    appInsightsConnectionString: appInsights.outputs.connectionString
    registryServer: registryServer
    uamiClientId: uami.outputs.clientId
    crmDataverseOrgUrl: dynamicsOrgUrl
    crmNativeWorkstreamIds: empty(dynamicsNativeMediaAdditionalWorkstreamIds) ? dynamicsNativeMediaWorkstreamId : '${dynamicsNativeMediaWorkstreamId},${dynamicsNativeMediaAdditionalWorkstreamIds}'
    ingestApiKey: ingestApiKey
    assistImage: assistImage
    assistOpenAiEndpoint: foundryOpenAiEndpoint
    assistOpenAiDeployment: assistModelDeploymentName
    assistInsightsDeployment: insightsModelDeploymentName
    knowledgeEndpoint: enableFoundryIq ? knowledge!.outputs.endpoint : ''
    knowledgeMode: foundryIqMode
  }
}

// ---------------- Orchestrator API (FastAPI) ----------------
module orchestratorApp 'br/public:avm/res/app/container-app:0.17.0' = if (deployWebChat) {
  name: 'orchestratorApp'
  params: {
    name: 'ca-orchestrator-api'
    location: hostingLocation
    workloadProfileName: 'Consumption'
    tags: union(tags, { 'azd-service-name': 'orchestrator-api' })
    environmentResourceId: acaEnv.outputs.resourceId
    managedIdentities: {
      userAssignedResourceIds: [uami.outputs.resourceId]
    }
    registries: [
      {
        server: registryServer
        identity: uami.outputs.resourceId
      }
    ]
    ingressExternal: true
    ingressTargetPort: 8000
    ingressAllowInsecure: false
    // The handover hub keeps queue + relay state in-memory, so the orchestrator must run as a
    // single replica (WebSockets pin to one replica but HTTP round-robins across replicas).
    scaleSettings: {
      minReplicas: 1
      maxReplicas: 1
    }
    secrets: concat(
      deployServiceApi ? [{
        name: 'assist-ingest-key'
        value: ingestApiKey
      }] : [],
      deployEmail ? [{
        name: 'acs-email-connection-string'
        value: emailConnectionString
      }] : []
    )
    containers: [
      {
        name: 'orchestrator-api'
        image: orchestratorImage
        resources: {
          cpu: json('0.5')
          memory: '1Gi'
        }
        env: [
          { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
          { name: 'AZURE_AI_MODEL_DEPLOYMENT_NAME', value: modelDeploymentName }
          { name: 'FOUNDRY_MODEL', value: modelDeploymentName }
          { name: 'FOUNDRY_AGENT_NAME', value: foundryAgentName }
          { name: 'ORCHESTRATOR_TARGET', value: 'local' }
          { name: 'AZURE_CLIENT_ID', value: uami.outputs.clientId }
          { name: 'ASSIST_BASE_URL', value: assistOrigin }
          { name: 'ASSIST_INGEST_KEY', ...(deployServiceApi ? { secretRef: 'assist-ingest-key' } : { value: '' }) }
          { name: 'ASSIST_ENABLED', value: string(deployAgentAssist) }
          { name: 'ACS_EMAIL_CONNECTION_STRING', ...(deployEmail ? { secretRef: 'acs-email-connection-string' } : { value: '' }) }
          { name: 'ACS_EMAIL_SENDER', value: acsEmailSender }
        ]
      }
    ]
  }
}

// ---------------- Chat UI (Next.js) ----------------
module uiApp 'br/public:avm/res/app/container-app:0.17.0' = if (deployUi) {
  name: 'uiApp'
  params: {
    name: 'ca-ui-v2'
    location: hostingLocation
    workloadProfileName: 'Consumption'
    tags: union(tags, { 'azd-service-name': 'ui-v2' })
    environmentResourceId: acaEnv.outputs.resourceId
    managedIdentities: {
      userAssignedResourceIds: [uami.outputs.resourceId]
    }
    registries: [
      {
        server: registryServer
        identity: uami.outputs.resourceId
      }
    ]
    ingressExternal: true
    ingressTargetPort: 8080
    ingressAllowInsecure: false
    scaleSettings: {
      minReplicas: 1
      maxReplicas: 3
    }
    containers: [
      {
        name: 'ui-v2'
        image: uiImage
        resources: {
          cpu: json('0.5')
          memory: '1Gi'
        }
        env: [
          { name: 'NEXT_PUBLIC_API_URL', value: orchestratorOrigin }
        ]
      }
    ]
  }
}

// ---------------- Operator (agent) console (Next.js) — handover queue + live chat ----------------
module agentConsoleApp 'br/public:avm/res/app/container-app:0.17.0' = if (deployAgentConsole) {
  name: 'agentConsoleApp'
  params: {
    name: 'ca-agent-console'
    location: hostingLocation
    workloadProfileName: 'Consumption'
    tags: union(tags, { 'azd-service-name': 'agent-console' })
    environmentResourceId: acaEnv.outputs.resourceId
    managedIdentities: {
      userAssignedResourceIds: [uami.outputs.resourceId]
    }
    registries: [
      {
        server: registryServer
        identity: uami.outputs.resourceId
      }
    ]
    ingressExternal: true
    ingressTargetPort: 8081
    ingressAllowInsecure: false
    scaleSettings: {
      minReplicas: 1
      maxReplicas: 1
    }
    containers: [
      {
        name: 'agent-console'
        image: agentConsoleImage
        resources: {
          cpu: json('0.5')
          memory: '1Gi'
        }
        env: [
          { name: 'NEXT_PUBLIC_API_URL', value: orchestratorOrigin }
        ]
      }
    ]
  }
}

// ---------------- Voice channel (aiohttp + ACS + HUMAIN/LiveKit) on the public env ----------------
// Derive the voice app's own public FQDN so it can tell ACS where to send call events + media.
var voiceFqdnComputed = 'ca-voice.${acaEnv.outputs.defaultDomain}'
var uamiResourceIdComputed = resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', '${abbrs.identity}${resourceToken}')

resource voiceApp 'Microsoft.App/containerApps@2024-10-02-preview' = if (deployVoice) {
  name: 'ca-voice'
  location: hostingLocation
  tags: union(tags, { 'azd-service-name': 'voice' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uamiResourceIdComputed}': {}
    }
  }
  properties: {
    managedEnvironmentId: acaEnv.outputs.resourceId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registryServer
          identity: uami.outputs.resourceId
        }
      ]
      secrets: concat(
        empty(humainAuthorization) ? [] : [{ name: 'humain-authorization', value: humainAuthorization }],
        empty(dynamicsOutboundApiKey) ? [] : [{ name: 'dynamics-outbound-key', value: dynamicsOutboundApiKey }],
        empty(dynamicsNativeProbeApiKey) ? [] : [{ name: 'native-call-probe-key', value: dynamicsNativeProbeApiKey }],
        empty(dynamicsNativeMediaApiKey) ? [] : [{ name: 'native-media-key', value: dynamicsNativeMediaApiKey }],
        empty(dynamicsNativeMediaAcsConnectionString) ? [] : [{ name: 'native-media-acs', value: dynamicsNativeMediaAcsConnectionString }],
        empty(voiceAcsConnectionString) ? [] : [{ name: 'acs-connection-string', value: voiceAcsConnectionString }],
        deployServiceApi ? [{ name: 'agent-assist-key', value: ingestApiKey }] : [],
        deployEmail ? [{ name: 'acs-email-connection-string', value: emailConnectionString }] : []
      )
    }
    template: {
      containers: [
        {
          name: 'voice'
          image: voiceImage
          resources: {
            // Scaled UP (not out): the realtime voice middle-tier keeps each call's session +
            // registry in-process, so it runs on a SINGLE replica (see scale block below). Give that
            // replica the Consumption-environment maximum (2 vCPU / 4 GiB) so one async worker can
            // handle the per-call LiveKit media bridges. Load-test against the HUMAIN room quota.
            cpu: json('2.0')
            memory: '4Gi'
          }
          env: [
            { name: 'PORT', value: '8000' }
            { name: 'CUSTOMER_UI_URL', value: uiOrigin }
            { name: 'ACS_CONNECTION_STRING', ...(empty(voiceAcsConnectionString) ? { value: '' } : { secretRef: 'acs-connection-string' }) }
            { name: 'ACS_SOURCE_NUMBER', value: acsSourceNumber }
            { name: 'ACS_AUTO_CONFERENCE_ENABLED', value: string(acsAutoConferenceEnabled) }
            { name: 'ACS_AUTO_CONFERENCE_NUMBER', value: acsAutoConferenceNumber }
            { name: 'ACS_CALLBACK_PATH', value: 'https://${voiceFqdnComputed}/acs/call-events' }
            { name: 'ACS_MEDIA_STREAMING_WEBSOCKET_PATH', value: 'wss://${voiceFqdnComputed}/realtime-acs' }
            { name: 'HUMAIN_AUTHORIZATION', ...(empty(humainAuthorization) ? { value: '' } : { secretRef: 'humain-authorization' }) }
            { name: 'HUMAIN_ENABLED', value: string(!empty(humainAuthorization)) }
            { name: 'HUMAIN_TOKEN_ENDPOINT', value: humainTokenEndpoint }
            { name: 'HUMAIN_VOICE', value: humainVoice }
            { name: 'HUMAIN_ASR_MODEL', value: humainAsrModel }
            { name: 'HUMAIN_LLM_PROVIDER', value: humainLlmProvider }
            { name: 'HUMAIN_INTERRUPTION_POLICY_MODE', value: humainInterruptionPolicyMode }
            { name: 'HUMAIN_TURN_TAKING_STRATEGY', value: humainTurnTakingStrategy }
            // LiveKit forwards HUMAIN transcripts before handover; ACS transcribes the
            // customer and operator after the AI media bridge is muted.
            { name: 'AZURE_COGNITIVE_SERVICES_ENDPOINT', value: foundrySpeechEndpoint }
            { name: 'ACS_TRANSCRIPTION_WEBSOCKET_PATH', value: 'wss://${voiceFqdnComputed}/transcription-acs' }
            { name: 'AZURE_TENANT_ID', value: tenant().tenantId }
            { name: 'AZURE_CLIENT_ID', value: uami.outputs.clientId }
            { name: 'DYNAMICS_ORG_URL', value: dynamicsOrgUrl }
            { name: 'DYNAMICS_OUTBOUND_CONFIG_ID', value: dynamicsOutboundConfigId }
            { name: 'DYNAMICS_OUTBOUND_ENABLED', value: string(dynamicsOutboundEnabled) }
            { name: 'DYNAMICS_OUTBOUND_API_KEY', ...(empty(dynamicsOutboundApiKey) ? { value: '' } : { secretRef: 'dynamics-outbound-key' }) }
            { name: 'DYNAMICS_NATIVE_PROBE_RESOURCE_ID', value: dynamicsNativeProbeResourceId }
            { name: 'DYNAMICS_NATIVE_PROBE_EXPIRES_AT', value: dynamicsNativeProbeExpiresAt }
            { name: 'DYNAMICS_NATIVE_PROBE_PERMANENT', value: string(dynamicsNativeProbePermanent) }
            { name: 'DYNAMICS_NATIVE_PROBE_API_KEY', ...(empty(dynamicsNativeProbeApiKey) ? { value: '' } : { secretRef: 'native-call-probe-key' }) }
            { name: 'DYNAMICS_NATIVE_MEDIA_ENABLED', value: string(dynamicsNativeMediaEnabled) }
            { name: 'DYNAMICS_NATIVE_MEDIA_TRANSPORT', value: dynamicsNativeMediaTransport }
            { name: 'DYNAMICS_NATIVE_MEDIA_FAST_START', value: string(dynamicsNativeMediaFastStart) }
            { name: 'DYNAMICS_NATIVE_MEDIA_WORKSTREAM_ID', value: dynamicsNativeMediaWorkstreamId }
            { name: 'DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS', value: dynamicsNativeMediaAdditionalWorkstreamIds }
            { name: 'DYNAMICS_NATIVE_SUPERVISION_ENABLED', value: string(dynamicsNativeSupervisionEnabled) }
            { name: 'DYNAMICS_NATIVE_SUPERVISION_ACTOR_IDS', value: dynamicsNativeSupervisionActorIds }
            { name: 'DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS', value: dynamicsNativeSupervisionControllers }
            { name: 'DYNAMICS_NATIVE_MEDIA_API_KEY', ...(empty(dynamicsNativeMediaApiKey) ? { value: '' } : { secretRef: 'native-media-key' }) }
            { name: 'DYNAMICS_NATIVE_MEDIA_ACS_CONNECTION_STRING', ...(empty(dynamicsNativeMediaAcsConnectionString) ? { value: '' } : { secretRef: 'native-media-acs' }) }
            { name: 'AGENT_ASSIST_BASE_URL', value: assistOrigin }
            { name: 'AGENT_ASSIST_API_KEY', ...(deployServiceApi ? { secretRef: 'agent-assist-key' } : { value: '' }) }
            { name: 'AGENT_ASSIST_ENABLED', value: string(deployAgentAssist) }
            { name: 'AGENT_PHONE_NUMBER', value: agentPhoneNumber }
            { name: 'ACS_EMAIL_CONNECTION_STRING', ...(deployEmail ? { secretRef: 'acs-email-connection-string' } : { value: '' }) }
            { name: 'ACS_EMAIL_SENDER', value: acsEmailSender }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.outputs.connectionString }
          ]
        }
      ]
      scale: {
        // MUST stay single-replica. The voice app holds per-call sessions (RTMiddleTier instances)
        // and the call-id -> session registry IN PROCESS MEMORY. A single phone call spans several
        // separate connections to ACS — the media-streaming WS (/realtime-acs), the transcription WS
        // (/transcription-acs) and the call-event webhooks (/acs/call-events) — plus the EventGrid
        // incoming-call POST. With >1 replica those land on different replicas and can't see the same
        // in-memory session, splitting the call (same constraint as the orchestrator handover hub).
        // We scale UP (CPU/memory above), not OUT. Moving to >1 replica requires externalizing session
        // ownership (e.g. per-call sticky routing or a shared media broker), which is out of scope here.
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output acrLoginServer string = registryServer
output acrName string = registry.outputs.name
output uamiClientId string = uami.outputs.clientId
output uamiPrincipalId string = uami.outputs.principalId
output keyVaultName string = keyVault.outputs.name
output orchestratorFqdn string = deployWebChat ? orchestratorApp!.outputs.fqdn : ''
output uiFqdn string = deployUi ? uiApp!.outputs.fqdn : ''
output agentConsoleFqdn string = deployAgentConsole ? agentConsoleApp!.outputs.fqdn : ''
output cosmosEndpoint string = deployServiceApi ? dataplane!.outputs.cosmosEndpoint : ''
output cosmosDatabaseName string = deployServiceApi ? dataplane!.outputs.cosmosDatabaseName : ''
output cosmosAccountName string = deployServiceApi ? dataplane!.outputs.cosmosAccountName : ''
output acaEnvVnetResourceId string = deployServiceApi ? dataplane!.outputs.acaEnvVnetResourceId : ''
output acaEnvVnetName string = deployServiceApi ? dataplane!.outputs.acaEnvVnetName : ''
output signalRName string = deployServiceApi ? dataplane!.outputs.signalRName : ''
output assistFqdn string = deployServiceApi ? dataplane!.outputs.assistFqdn : ''
output crmUamiClientId string = deployServiceApi ? dataplane!.outputs.crmUamiClientId : ''
output crmUamiPrincipalId string = deployServiceApi ? dataplane!.outputs.crmUamiPrincipalId : ''
output crmUamiResourceId string = deployServiceApi ? dataplane!.outputs.crmUamiResourceId : ''
output voiceFqdn string = deployVoice ? voiceApp!.properties.configuration.ingress.fqdn : ''
output acsEmailSender string = acsEmailSender
output acsName string = deployCommunications ? acsEmail!.name : ''
output foundryAccountName string = deployFoundry ? foundry!.outputs.accountName : ''
output foundryProjectEndpoint string = foundryProjectEndpoint
output openAiEndpoint string = foundryOpenAiEndpoint
output cognitiveServicesEndpoint string = foundrySpeechEndpoint
output knowledgeSearchEndpoint string = enableFoundryIq ? knowledge!.outputs.endpoint : ''
