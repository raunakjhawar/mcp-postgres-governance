# Security model and safe use

This teaching sample intentionally separates network reachability, caller identity,
tool permissions, and PostgreSQL privileges.

## Trust boundaries

- The public MCP URL is APIM, reachable from the internet; its tools require Entra tokens.
- APIM registers the existing server as a native MCP API. Native inventory does
  not replace backend authentication, tool authorisation, or database isolation.
- Both App Service applications have public network access disabled. The old
  direct public endpoint cannot bypass APIM from the internet.
- APIM Developer is injected into a dedicated subnet in external mode. NSG rules
  allow gateway HTTPS, platform management, and load-balancer probes.
- APIM and the private backend intentionally share the same logical API audience;
  the backend revalidates the forwarded token. No caller identity header is trusted.
- Authorised clients inside the VNet can directly call the private backend. They
  bypass APIM's counter but not backend authentication, tool permissions, or RLS.
- Both applications connect privately to PostgreSQL using distinct managed identities.
- The bootstrap identity has database administrator authority and is not a runtime identity.
- The LLM/client and tool arguments are untrusted. Subject and tenant come from verified claims.
- RLS trusts application-set transaction context. It does not contain a fully compromised server.
- Local mode is an explicit, loopback-only authentication bypass for synthetic data.

Do not attach the bootstrap identity to an application or grant runtime identities
ownership, administrator roles, `BYPASSRLS`, or broad schema privileges.

## Before production

Perform your own security review, tenant/client compatibility checks, threat model,
dependency review, data classification, recovery design, and cost review.
Add distributed limits, central security telemetry, appropriate edge controls,
workload-specific network restrictions, and a tested identity lifecycle.
APIM Developer is not a production SLA tier. No log-body capture or central APIM
diagnostic sink is provisioned by this sample; configure redacted telemetry separately.

Do not expose an arbitrary SQL tool or use production employee records.
For sensitive writes, implement operation-bound approvals in addition to roles.

## Reporting issues

Do not disclose credentials, access tokens, tenant diagnostic exports, or personal
data in public issues. Contact your repository maintainer through an approved
private channel. If you publish a fork, configure your own private reporting policy.
