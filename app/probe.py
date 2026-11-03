import asyncio
import ipaddress
import os
import socket

import httpx

from app.client import exercise


async def main():
    url = os.environ["PRIVATE_URL"]
    hostname = httpx.URL(url).host
    addresses = await asyncio.to_thread(socket.gethostbyname_ex, hostname)
    if not addresses[2] or not all(
        ipaddress.ip_address(address).is_private for address in addresses[2]
    ):
        raise RuntimeError("Private endpoint did not resolve to private addresses")
    print("PASS private MCP DNS resolves to private address")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        for attempt in range(30):
            try:
                health = await client.get(url + "/health")
                if health.status_code == 200 and health.json().get("mode") == "azure":
                    break
                if attempt == 29:
                    raise RuntimeError(f"Private app unhealthy: HTTP {health.status_code}")
            except httpx.TransportError:
                if attempt == 29:
                    raise
            print(f"Waiting for private app startup ({attempt + 1}/30)")
            await asyncio.sleep(10)
        print("PASS private MCP reachable from VNet")
        denied = await client.get(url + "/mcp")
        if denied.status_code != 401:
            raise RuntimeError(f"Private unauthenticated request not denied: {denied.status_code}")
        print("PASS private MCP still requires authentication")
    token = os.environ.get("MCP_ACCESS_TOKEN")
    if token:
        await exercise(url, token)
    else:
        print(
            "NOT ASSESSED authenticated private tool invocation: no delegated test token supplied"
        )


if __name__ == "__main__":
    asyncio.run(main())
