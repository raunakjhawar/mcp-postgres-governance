# Private Database, Public Tools: runnable Azure MCP sample

## Architecture

```text
Assigned Entra user + delegated access token
                 |
        +--------+-----------------------+
        |                                |
Public HTTPS APIM                 Private-network client
Developer, external VNet mode            |
        |                                |
        +------ Private MCP App Service -+
                Private endpoint
                Public access disabled
                         |
              Private PostgreSQL 16
              Public network access disabled
              Entra-only database authentication
```

Resources share a single demo resource group:

- One Linux Basic B1 App Service plan, with two applications.
- One APIM Developer instance in external VNet mode, with a dedicated subnet and NSG.
- One PostgreSQL Burstable B1ms server, 32 GiB storage, no HA.
- One Basic Azure Container Registry.
- One VNet, delegated subnets, private DNS zones, and an App Service private endpoint.
- Separate managed identities for each runtime and database bootstrap.
- Short-lived Azure Container Instances for bootstrap and private-network verification.
- Two Entra app registrations: MCP API and reader CLI.

**This is a security teaching sample, not a production reference architecture or
multi-region deployment.** Resources incur ongoing charges. Check current prices
for your region and agreement before deployment. Stopping an application does not
delete the plan, APIM instance, database storage, registry, or private endpoint.
APIM Developer has **no production SLA** and is approved here for demonstration only.
Initial APIM provisioning commonly takes 30-60 minutes or longer.

The original App Service named `<prefix>-public` is retained to avoid destructive
resource migration, but **its public network access is disabled**. It is not the
gateway backend. APIM routes to `<prefix>-private` through its existing private endpoint.

## Tools and permission boundaries

| Tool | Reader | Operator | Data scope |
|---|---|---|---|
| `get_my_employee_profile` | Yes | Yes | Verified caller's synthetic profile |
| `list_my_requests` | Yes | Yes | At most 20 requests for the caller |
| `create_support_request` | No | Yes | Synthetic request with idempotency UUID |

The deployment assigns **only Reader** to the deploying user. Operator is opt-in.
All authenticated users must have an explicit Reader or Operator assignment.
There is no arbitrary SQL tool, real HR integration, or external ticket creation.

Security mechanisms:

- RS256 signature, issuer, audience, tenant, lifetime, delegated scope, and app-role validation.
- APIM also validates the API's Entra audience, issuer, tenant, scope, and Reader/Operator
  role before forwarding protected requests. The backend still independently validates
  tokens and enforces individual tool permissions.
- APIM limits protected requests to 30/minute per tenant/subject, with HTTP 429.
- Enterprise application assignment required; no anonymous tool invocation.
- Managed identity database connections; no deployed database password.
- Fixed parameterised SQL and projected profile fields.
- PostgreSQL column permissions deny the synthetic salary field.
- Transaction-local tenant/subject context with row-level security.
- Maximum four database operations per application process, 5-second statement
  timeout, 2-second lock timeout, and a 12-second operation budget.
- Managed identity token acquisition is warmed during application startup with a
  separate 60-second deadline, rather than consuming the first tool call's budget.
- Per-process, per-subject request limit: 60 protected HTTP requests/minute.
- Non-root application container; no access-token or response-body logging.

## Prerequisites

- Python 3.12 and Git.
- Azure CLI with Bicep installed and an interactive **user** sign-in.
- Azure rights to create resources and role assignments in the selected subscription
  (Contributor alone cannot create Azure RBAC role assignments).
- Directory permission to create app registrations/service principals and assign
  application roles. Some tenants require an administrator for this.
- Docker Desktop with Compose **only for the local PostgreSQL demo**.
  Azure deployment uses ACR's remote build; local Docker is not required.
- A region supporting the selected PostgreSQL SKU, Linux App Service, APIM Developer,
  and private ACI.
- An APIM publisher email: the script uses the signed-in user's `mail`, or you can
  supply `-PublisherEmail demo-owner@example.com`.

Tenant policies, Conditional Access, restricted app registration, resource policies,
regional capacity, or quota can prevent deployment. The scripts report errors;
they do not weaken controls or open the database as a fallback.

## Install and test

From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements.lock -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
az bicep build --file .\infra\main.bicep --stdout | Out-Null
```

The tests exercise the real MCP protocol in-process with a fake database.
They do **not** substitute for Azure/database integration verification.
CI also runs the database bootstrap, column-permission checks, and RLS tests
against a disposable PostgreSQL 16 service. To run that test yourself, set
`TEST_POSTGRES_DSN` to an empty disposable database whose administrator can create
roles. It is skipped when that variable is absent. Never point it at production.

## Deploy both Azure environments

Verify the subscription and tenant before provisioning:

```powershell
az login --tenant YOUR_TENANT_ID
az account set --subscription YOUR_SUBSCRIPTION_ID
az account show --query "{subscription:name,subscriptionId:id,tenantId:tenantId}"
.\scripts\deploy.ps1 -Location westeurope -ApproveCosts
```

If Windows' default execution policy blocks unsigned local scripts, and your
organisation permits running them, use a process-local invocation:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\deploy.ps1 -ApproveCosts
```

Do not override an organisation-enforced execution policy.

The script:

1. Records deployment state under ignored `.deployment/`.
2. Creates single-tenant app registrations and exposes `access_as_user`.
3. Preauthorises the sample reader client and Azure CLI to request that scope.
4. Requires enterprise application assignment and assigns the deploying user Reader.
5. Deploys the VNet, private database, managed identities, registry, private apps,
   and APIM Developer gateway; disables public access to the former direct endpoint.
6. Builds the container remotely in your Azure Container Registry.
7. Runs a private bootstrap container to create tables, RLS policies, runtime
   database principals, synthetic rows, and database permission assertions.
8. Deletes the successful bootstrap container.

The bootstrap identity is the database Entra administrator. It is **not attached
to either running web application**. The private verification runner instead uses
the private application's restricted runtime identity for registry pull.
Treat permission to create bootstrap containers with the administrator identity
as privileged administration.

The script can resume from saved state after an error. Re-run it rather than
creating new registrations manually. Do not delete state while resources exist.
If provisioning fails part-way, resources may still be billable; inspect the
resource group and use the removal script when appropriate.
If the existing demo PostgreSQL server was stopped, deployment starts it before
applying configuration. Existing application updates build the image before changing
its tag to avoid an image-not-found startup failure.

After changing source, deploy a new image tag rather than relying on a cached image:

```powershell
.\scripts\deploy.ps1 -ApproveCosts -ImageTag v2
```

### Authentication compatibility

The sample implements bearer-token protection and protected resource metadata.
APIM advertises the **gateway** MCP resource and metadata URLs. The backend
retains its own metadata for direct private clients. The API application ID/audience
is intentionally shared by these two entry points to the same logical MCP service:
APIM forwards the unchanged token and the backend validates the same audience.
This is not forwarding a token issued for an unrelated backend.
Its supplied CLI uses a pre-registered public client and device sign-in.
It does not implement dynamic client registration or promise compatibility with
every hosted Copilot connector. Check your client's registration, consent, transport,
and private-network support before integration.

Azure CLI is preauthorised solely to simplify developer verification. Reader app
roles remain required. Remove that preauthorisation if your deployment policy
requires all calls to come from a separately approved application.

### Gateway implementation

[infra/apim.bicep](infra/apim.bicep) registers an **existing MCP server** natively
(`type: mcp`), not APIM's REST-to-MCP tool generation. In the Azure portal, open
your API Management instance, then **APIs > MCP Servers > Private PostgreSQL MCP**.
The Python backend still owns tool discovery, schemas, and tool execution.
The public MCP URL remains `/mcp`.

[infra/apim-policy.xml](infra/apim-policy.xml) configures:

- Native MCP routing to the explicit `private-mcp` backend.
- Separate HTTP APIs for public `/health` and `/.well-known/oauth-protected-resource`.
- JWT validation and subject-based gateway throttling on protected MCP requests.
- The private App Service hostname for backend routing and TLS validation.
- Inherited native backend processing, without HTTP-proxy rewrite or Host overrides.
- Gateway-aware `WWW-Authenticate` challenges for rejected tokens.

Authorization, MCP session/protocol headers, and request bodies are passed through;
the application accepts its configured gateway origin in addition to its private origin.
The sample uses stateless JSON responses. Do not assume it has validated long-lived
SSE notifications or every external MCP client's streaming behaviour.

The native registration uses a named `message` endpoint at `/mcp`; no general
wildcard HTTP backend route is added. The backend URL is the private origin, not
the origin plus `/mcp`, which would duplicate the path.
The service's preview endpoint-map contract differs from published array schemas.
APIM currently advertises `//.well-known/oauth-protected-resource` in native
authentication challenges for an empty API path. Both that advertised URL and
the canonical single-slash URL serve the same metadata; verification checks both.
Health and OAuth discovery are public and do not consume the authenticated MCP limit.
The throttle test deliberately consumes the test user's gateway budget; allow at
least 60 seconds before immediately rerunning authenticated checks.

To migrate an existing sample deployment without rebuilding the app or database:

```powershell
.\scripts\update-gateway.ps1
.\scripts\verify.ps1
```

The migration removes only the five known legacy proxy operations. It stops if
unexpected operations exist rather than deleting custom configuration.

## Verify the deployed environments

Run from a machine **outside** the deployed VNet:

```powershell
.\scripts\verify.ps1
```

Verification checks:

- APIM is successfully provisioned in Developer external VNet mode.
- Management-plane inventory contains a native MCP API with the expected backend
  and message endpoint, rather than only an HTTP API.
- Gateway health reaches a backend running Azure authentication mode.
- Anonymous and malformed-token MCP requests receive HTTP 401.
- Both the private App Service and former public App Service reject internet access
  with HTTP 403, preventing public gateway bypass.
- OAuth metadata and authentication challenges advertise the gateway resource.
- Assigned-user authentication and MCP protocol initialisation work publicly.
- The caller receives the expected synthetic profile without the salary column.
- Reader cannot invoke the write tool.
- A temporary VNet-connected container resolves private DNS and reaches the private app.
- The private endpoint also requires authentication and serves the same tools.
- A bounded series of MCP GETs receives HTTP 429 from the gateway's 30-request limit,
  without querying the database.

The delegated test token is passed to the private runner as an ACI `secureValue`,
never as a command-line argument or committed configuration. The request file is
deleted and the runner is deleted after verification.
Results are saved to ignored `.deployment/verification.json` only after all
selected checks succeed.

If the Azure CLI cannot silently acquire the new API scope, authenticate using
your organisation's approved process. To test public tools separately with the
reader device flow:

```powershell
.\.venv\Scripts\python.exe -m app.client
```

For network checks without a user token:

```powershell
.\scripts\verify.ps1 -NetworkOnly
```

Network-only verification explicitly leaves authenticated tool access **not
assessed**. Do not describe that outcome as full functional verification.

## Opt in to the Operator role

In Entra admin center, find the sample MCP API enterprise application and assign
the **Operator** role to an approved test user under Users and groups.

The synthetic profile is initially seeded only for the deploying user. Additional
assigned users need their own synthetic row provisioned by the administrator.
Never add real employee information to this demo.

After acquiring a fresh token with the Operator role:

```powershell
.\.venv\Scripts\python.exe -m app.client --operator
```

The client creates one synthetic request, retries the same UUID, and verifies
that the retry does not create another request.

## Local demo

Docker must be running:

```powershell
.\scripts\local.ps1
```

The script generates random local-only passwords into ignored
`.deployment/local.env`, starts PostgreSQL on `127.0.0.1:5433`, bootstraps the
schema, and starts MCP on `127.0.0.1:8000`.

In a second terminal:

```powershell
.\.venv\Scripts\python.exe -m app.client --local
```

**Local mode intentionally bypasses Entra authentication** using one synthetic
Operator identity. It binds only to loopback and is rejected on Azure App Service.
Do not reverse-proxy, tunnel, or expose local mode.

Stop the server with Ctrl+C. Stop the local database with:

```powershell
docker compose --env-file .\.deployment\local.env down
```

The local volume persists synthetic records. Do not delete its password file
while retaining the volume unless you also manage the database password change.

## Important limitations

- RLS uses context set by trusted application code, not independently verified
  database end-user identity. A compromised runtime able to issue arbitrary SQL
  can change that context. This is not a defence against a fully compromised app.
- The runtime can insert synthetic requests at the database layer; Reader versus
  Operator enforcement is an application control, tested through MCP.
- Backend rate limiting and concurrency remain per process. APIM provides the
  gateway limit; its counters are not an exact billing quota and may be approximate.
  Direct authorised private clients do not traverse that gateway counter.
- Health is liveness only; it does not prove PostgreSQL or model readiness.
  Startup validates managed identity token acquisition, not database readiness.
- The sample uses the standard `postgres` database for demo tables, not a
  dedicated production application database.
- No production WAF, central immutable audit archive, SIEM, HA, backup-recovery
  exercise, egress appliance, or multi-region failover is provisioned.
- There is no deliberately vulnerable public endpoint. Demonstrations use
  negative tests rather than exposing an unsafe database tool.
- The private endpoint's authenticated tool path must be tested from a supported
  network; a public URL and Entra token alone do not provide private routing.

See [SECURITY.md](SECURITY.md) for the trust model and publication checklist.

## Cleanup

Inspect `.deployment/state.json` to confirm the exact subscription and resource
group, then:

```powershell
.\scripts\remove.ps1 -ConfirmDelete
```

This deletes the **sample resource group and the two sample app registrations**.
APIM is in that resource group and is deleted as part of cleanup.
It does not delete the repository or unrelated resources. Resource locks or
directory policy can block cleanup; investigate any failure rather than assuming
charges have stopped.

## Share with readers

This repository is MIT-licensed. Publish only source files and documentation.
Never publish `.deployment/`, virtual environments, generated manifests containing
identifiers, local passwords, tokens, or Azure diagnostic output.

No GitHub repository is created by the deployment scripts. Readers deploy into
their own tenant/subscription and receive independent app registrations and data.
