import argparse
import asyncio
import json
import os
import re
from pathlib import Path

import httpx


async def check_gateway(url: str, audience: str, token: str | None, throttle: bool):
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        metadata_url = url + "/.well-known/oauth-protected-resource"
        response = await client.get(metadata_url)
        response.raise_for_status()
        metadata = response.json()
        if metadata["resource"] != url + "/mcp":
            raise RuntimeError("Gateway metadata advertises the wrong MCP resource")
        if metadata["scopes_supported"] != [f"api://{audience}/access_as_user"]:
            raise RuntimeError("Gateway metadata advertises unexpected scopes")
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            response = await client.get(url + "/mcp", headers=headers)
            if response.status_code != 401:
                raise RuntimeError(f"Gateway did not reject invalid authentication: {response}")
            challenge = re.search(
                r'\bresource_metadata="([^"]+)"', response.headers.get("WWW-Authenticate", "")
            )
            # Native APIM currently adds a second slash for an API with an empty path.
            if not challenge or challenge[1] not in {
                metadata_url,
                url + "//.well-known/oauth-protected-resource",
            }:
                raise RuntimeError("Authentication challenge does not advertise gateway metadata")
            advertised = await client.get(challenge[1])
            advertised.raise_for_status()
            if advertised.json() != metadata:
                raise RuntimeError("Advertised gateway metadata differs from canonical metadata")
        print("PASS gateway OAuth metadata and authentication challenges")
        if throttle:
            if not token:
                raise RuntimeError("Delegated token required to verify gateway throttling")
            # No database queries: only bounded GETs on the stateless MCP transport.
            for _ in range(35):
                response = await client.get(
                    url + "/mcp", headers={"Authorization": f"Bearer {token}"}
                )
                if response.status_code == 429:
                    if "Retry-After" not in response.headers:
                        raise RuntimeError("Gateway throttling omitted Retry-After")
                    print("PASS gateway enforces its 30-request/minute subject limit")
                    return
                if response.status_code not in {200, 405, 406}:
                    raise RuntimeError(f"Unexpected status during throttle check: {response}")
            raise RuntimeError("Gateway did not enforce its configured request limit")


def main():
    parser = argparse.ArgumentParser(description="Verify APIM OAuth discovery and gateway limit")
    parser.add_argument("--state", default=".deployment/state.json")
    parser.add_argument("--throttle", action="store_true")
    args = parser.parse_args()
    state = json.loads(Path(args.state).read_text(encoding="utf-8-sig"))
    asyncio.run(
        check_gateway(
            state["outputs"]["publicUrl"]["value"],
            state["apiAppId"],
            os.environ.get("MCP_ACCESS_TOKEN"),
            args.throttle,
        )
    )


if __name__ == "__main__":
    main()
