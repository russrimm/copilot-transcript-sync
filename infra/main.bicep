//
// Copilot Studio transcript sync -- Azure infrastructure.
//
// Deploys the Function App (Flex Consumption, Python 3.13), an Azure Data
// Explorer cluster and database, a storage account for watermarks and
// deployment, and a user-assigned managed identity wired to all of it.
//
// The managed identity is the only identity granted anything in Azure. Power
// Platform access is federated onto a separate Entra app registration, created
// by scripts/bootstrap-entra.ps1 -- deliberately not modeled here, because
// creating the app registration and registering it as a Power Platform
// management application both require an interactive tenant administrator.
//

targetScope = 'resourceGroup'

@description('Base name used to derive resource names. Lowercase alphanumeric, 3-8 characters.')
@minLength(3)
@maxLength(8)
param namePrefix string = 'cts'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Client ID of the Entra app registration used to reach Power Platform and Dataverse. Created by scripts/bootstrap-entra.ps1.')
param powerPlatformAppClientId string

@description('Entra tenant ID hosting the Power Platform environments.')
param powerPlatformTenantId string = subscription().tenantId

@description('Optional. Object ID of an ADDITIONAL principal to grant Admin on the ADX database. Leave empty in most cases: Azure Data Explorer already grants Admin to the identity that deploys the database, so setting this to your own object ID fails with "a PrincipalAssignment already exists with the same role and principal id".')
param adxAdminPrincipalId string = ''

@description('Principal type of adxAdminPrincipalId.')
@allowed(['User', 'Group', 'App'])
param adxAdminPrincipalType string = 'User'

@description('ADX SKU. The default is the cheapest dev tier and carries no SLA; use a Standard SKU for production.')
param adxSkuName string = 'Dev(No SLA)_Standard_E2a_v4'

@description('ADX SKU tier. Must be Basic for a Dev SKU.')
@allowed(['Basic', 'Standard'])
param adxSkuTier string = 'Basic'

@description('ADX instance count. Dev SKUs support only 1.')
param adxCapacity int = 1

@description('Hot cache period for the ADX database.')
param adxHotCachePeriod string = 'P90D'

@description('Soft delete (retention) period for the ADX database. Must exceed the 30-day Dataverse retention this pipeline exists to outlive.')
param adxSoftDeletePeriod string = 'P730D'

@description('Timer schedule (NCRONTAB) for the sync. Defaults to every 30 minutes.')
param syncSchedule string = '0 */30 * * * *'

@description('Days of history to pull the first time an environment is seen.')
param initialBackfillDays int = 30

@description('Minutes of overlap replayed before the stored watermark on each run.')
param watermarkLookbackMinutes int = 120

@description('Whether the Function should auto-provision the Dataverse application user in newly discovered environments.')
param autoProvisionAppUser bool = true

var uniquePart = uniqueString(resourceGroup().id)
// Storage account names cap at 24 characters and ADX cluster names at 22, so
// both use a shortened suffix: 2 + 8 + 8 = 18 and 3 + 8 + 8 = 19 respectively.
var shortUnique = take(uniquePart, 8)
var storageName = toLower('st${namePrefix}${shortUnique}')
var adxClusterName = toLower('adx${namePrefix}${shortUnique}')
var functionAppName = '${namePrefix}-sync-${uniquePart}'
var planName = '${namePrefix}-plan-${uniquePart}'
var identityName = '${namePrefix}-id-${uniquePart}'
var workspaceName = '${namePrefix}-log-${uniquePart}'
var insightsName = '${namePrefix}-appi-${uniquePart}'

var adxDatabaseName = 'CopilotTranscripts'
var deploymentContainerName = 'app-package'
var watermarkTableName = 'SyncWatermarks'

// Verified with `az role definition list --name '<role>' --query "[0].name"`.
var roleStorageBlobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var roleStorageQueueDataContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var roleStorageTableDataContributor = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var roleMonitoringMetricsPublisher = '3913510d-42f4-4e42-8a64-420c390055eb'

// -------------------------------------------------------------------------
// Identity
// -------------------------------------------------------------------------

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

// -------------------------------------------------------------------------
// Storage: Function deployment package plus the watermark table
// -------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    // The Function and the sync job both authenticate with the managed
    // identity, so shared keys are never needed.
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    publicNetworkAccess: 'Enabled'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource watermarkTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: watermarkTableName
}

// -------------------------------------------------------------------------
// Observability
// -------------------------------------------------------------------------

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: insightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
  }
}

// -------------------------------------------------------------------------
// Azure Data Explorer
// -------------------------------------------------------------------------

resource adxCluster 'Microsoft.Kusto/clusters@2024-04-13' = {
  name: adxClusterName
  location: location
  sku: {
    name: adxSkuName
    tier: adxSkuTier
    capacity: adxCapacity
  }
  properties: {
    enableStreamingIngest: false
    enableDiskEncryption: true
    publicNetworkAccess: 'Enabled'
  }
}

resource adxDatabase 'Microsoft.Kusto/clusters/databases@2024-04-13' = {
  parent: adxCluster
  name: adxDatabaseName
  location: location
  kind: 'ReadWrite'
  properties: {
    hotCachePeriod: adxHotCachePeriod
    softDeletePeriod: adxSoftDeletePeriod
  }
}

// The Function only ever writes; Ingestor is the least-privileged role that allows that.
resource adxIngestor 'Microsoft.Kusto/clusters/databases/principalAssignments@2024-04-13' = {
  parent: adxDatabase
  name: guid(adxDatabase.id, identity.id, 'Ingestor')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'App'
    role: 'Ingestor'
    tenantId: subscription().tenantId
  }
}

// Viewer lets the Function verify its own ingestion; drop this if not wanted.
resource adxViewer 'Microsoft.Kusto/clusters/databases/principalAssignments@2024-04-13' = {
  parent: adxDatabase
  name: guid(adxDatabase.id, identity.id, 'Viewer')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'App'
    role: 'Viewer'
    tenantId: subscription().tenantId
  }
}

// Optional extra Admin. The deploying identity already receives Admin on the
// database from Azure Data Explorer itself, so this is only for granting an
// additional user, group, or CI principal.
resource adxAdmin 'Microsoft.Kusto/clusters/databases/principalAssignments@2024-04-13' = if (!empty(adxAdminPrincipalId)) {
  parent: adxDatabase
  name: guid(adxDatabase.id, adxAdminPrincipalId, 'Admin')
  properties: {
    principalId: adxAdminPrincipalId
    principalType: adxAdminPrincipalType
    role: 'Admin'
    tenantId: subscription().tenantId
  }
}

// -------------------------------------------------------------------------
// RBAC for the managed identity
// -------------------------------------------------------------------------

resource blobOwnerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, roleStorageBlobDataOwner)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobDataOwner)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource queueContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, roleStorageQueueDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageQueueDataContributor)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource tableContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, roleStorageTableDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageTableDataContributor)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource metricsPublisherAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: insights
  name: guid(insights.id, identity.id, roleMonitoringMetricsPublisher)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleMonitoringMetricsPublisher)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// -------------------------------------------------------------------------
// Function App (Flex Consumption)
// -------------------------------------------------------------------------

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.id
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.13'
      }
    }
    siteConfig: {
      appSettings: [
        // Identity-based connection to the host storage account. No keys.
        {
          name: 'AzureWebJobsStorage__blobServiceUri'
          value: storage.properties.primaryEndpoints.blob
        }
        {
          name: 'AzureWebJobsStorage__queueServiceUri'
          value: storage.properties.primaryEndpoints.queue
        }
        {
          name: 'AzureWebJobsStorage__tableServiceUri'
          value: storage.properties.primaryEndpoints.table
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'AzureWebJobsStorage__clientId'
          value: identity.properties.clientId
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: insights.properties.ConnectionString
        }
        {
          name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING'
          value: 'ClientId=${identity.properties.clientId};Authorization=AAD'
        }
        {
          name: 'UAMI_CLIENT_ID'
          value: identity.properties.clientId
        }
        {
          name: 'PP_TENANT_ID'
          value: powerPlatformTenantId
        }
        {
          name: 'PP_APP_CLIENT_ID'
          value: powerPlatformAppClientId
        }
        {
          name: 'ADX_CLUSTER_URI'
          value: adxCluster.properties.uri
        }
        {
          name: 'ADX_INGEST_URI'
          value: adxCluster.properties.dataIngestionUri
        }
        {
          name: 'ADX_DATABASE'
          value: adxDatabaseName
        }
        {
          name: 'ADX_RAW_TABLE'
          value: 'CopilotTranscriptRaw'
        }
        {
          name: 'WATERMARK_TABLE_ENDPOINT'
          value: storage.properties.primaryEndpoints.table
        }
        {
          name: 'WATERMARK_TABLE_NAME'
          value: watermarkTableName
        }
        {
          name: 'SYNC_SCHEDULE'
          value: syncSchedule
        }
        {
          name: 'INITIAL_BACKFILL_DAYS'
          value: string(initialBackfillDays)
        }
        {
          name: 'WATERMARK_LOOKBACK_MINUTES'
          value: string(watermarkLookbackMinutes)
        }
        {
          name: 'DATAVERSE_PAGE_SIZE'
          value: '25'
        }
        {
          name: 'AUTO_PROVISION_APP_USER'
          value: string(autoProvisionAppUser)
        }
        {
          name: 'EXCLUDED_ENVIRONMENT_TYPES'
          value: 'Developer,Teams'
        }
      ]
    }
  }
  dependsOn: [
    blobOwnerAssignment
    queueContributorAssignment
    tableContributorAssignment
    deploymentContainer
    watermarkTable
  ]
}

// -------------------------------------------------------------------------
// Outputs
// -------------------------------------------------------------------------

output functionAppName string = functionApp.name
output functionAppHostName string = functionApp.properties.defaultHostName
output identityClientId string = identity.properties.clientId
@description('Use this value as the federated identity credential subject on the Entra app registration.')
output identityPrincipalId string = identity.properties.principalId
output adxClusterUri string = adxCluster.properties.uri
output adxIngestUri string = adxCluster.properties.dataIngestionUri
output adxDatabaseName string = adxDatabaseName
output storageAccountName string = storage.name
output watermarkTableEndpoint string = storage.properties.primaryEndpoints.table
