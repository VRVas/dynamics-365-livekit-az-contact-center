from azure.identity import AzureDeveloperCliCredential, DefaultAzureCredential, ManagedIdentityCredential
import os
from azure.storage.blob.aio import BlobServiceClient

def get_azure_credentials(
    tenant_id: str | None = None,
    *,
    warmup_scope: str | None = "https://cognitiveservices.azure.com/.default",
) -> AzureDeveloperCliCredential | DefaultAzureCredential | ManagedIdentityCredential:
    credentials: AzureDeveloperCliCredential | DefaultAzureCredential | ManagedIdentityCredential | None = None

    # In Azure Container Apps we run under a user-assigned managed identity (UAMI).
    # ACA injects the UAMI's client id as AZURE_CLIENT_ID. ManagedIdentityCredential
    # MUST receive that client_id explicitly, otherwise ACA's IMDS endpoint returns
    # 400 "Unable to load the proper Managed Identity" (there is no system-assigned
    # identity to fall back to).
    uami_client_id = os.environ.get("AZURE_CLIENT_ID")
    if uami_client_id:
        print(f"Using ManagedIdentityCredential with client_id {uami_client_id}")
        credentials = ManagedIdentityCredential(client_id=uami_client_id)
    elif tenant_id is not None:
        print(f"Using AzureDeveloperCliCredential with tenant_id {tenant_id}")
        credentials = AzureDeveloperCliCredential(tenant_id=tenant_id, process_timeout=60)
    else:
        print("Using DefaultAzureCredential")
        credentials = DefaultAzureCredential()

    # Warm up before we start getting requests (cognitive services is the scope the
    # realtime + transcription clients actually use).
    if warmup_scope is not None:
        credentials.get_token(warmup_scope)
    return credentials



async def fetch_prompt_from_azure_storage(container_name: str, file_name: str) -> str:
    """
    Fetches a prompt as text from the specified container in Azure Storage.
    """
    connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not connection_string:
        raise ValueError("Missing 'AZURE_STORAGE_CONNECTION_STRING' environment variable.")

    blob_service_client = BlobServiceClient.from_connection_string(connection_string)
    container_client = blob_service_client.get_container_client(container_name)
    blob_client = container_client.get_blob_client(file_name)

    blob_data = await blob_client.download_blob()
    content = await blob_data.readall()
    return content.decode("utf-8")