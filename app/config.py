import os
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import UUID


@dataclass(frozen=True)
class Settings:
    mode: str
    tenant_id: str
    audience: str
    public_url: str
    db_host: str
    db_name: str
    db_user: str
    identity_client_id: str
    local_password: str
    requests_per_minute: int = 60
    gateway_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.environ.get("APP_MODE", "azure")
        if mode not in {"azure", "local"}:
            raise ValueError("APP_MODE must be azure or local")
        if mode == "local" and os.environ.get("WEBSITE_INSTANCE_ID"):
            raise ValueError("Local authentication is forbidden on Azure App Service")
        tenant = os.environ.get("TENANT_ID", "11111111-1111-1111-1111-111111111111")
        audience = os.environ.get("API_CLIENT_ID", "")
        url = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8000")
        if mode == "azure":
            UUID(tenant)
            UUID(audience)
            if urlparse(url).scheme != "https":
                raise ValueError("Azure PUBLIC_URL must use HTTPS")
        elif urlparse(url).hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("Local mode requires a loopback PUBLIC_URL")
        host = os.environ.get("PGHOST", "localhost")
        gateway_url = os.environ.get("GATEWAY_URL", "").rstrip("/")
        if gateway_url and (
            mode != "azure"
            or urlparse(gateway_url).scheme != "https"
            or not urlparse(gateway_url).hostname
        ):
            raise ValueError("GATEWAY_URL requires Azure mode and an HTTPS origin")
        password = os.environ.get("PGPASSWORD", "")
        if mode == "azure" and (password or not os.environ.get("AZURE_CLIENT_ID")):
            raise ValueError("Azure mode requires managed identity, not PGPASSWORD")
        return cls(
            mode=mode,
            tenant_id=tenant,
            audience=audience,
            public_url=url.rstrip("/"),
            db_host=host,
            db_name=os.environ.get("PGDATABASE", "mcpdemo"),
            db_user=os.environ.get("PGUSER", "mcp_local"),
            identity_client_id=os.environ.get("AZURE_CLIENT_ID", ""),
            local_password=password,
            requests_per_minute=int(os.environ.get("REQUESTS_PER_MINUTE", "60")),
            gateway_url=gateway_url,
        )
