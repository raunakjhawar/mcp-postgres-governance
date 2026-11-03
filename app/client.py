import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import msal
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def exercise(url: str, token: str | None, operator: bool = False):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers, timeout=30) as http_client:
        async with streamable_http_client(url.rstrip("/") + "/mcp", http_client=http_client) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                if names != {
                    "get_my_employee_profile",
                    "list_my_requests",
                    "create_support_request",
                }:
                    raise RuntimeError(f"Unexpected inventory: {names}")
                print("PASS protocol initialization and tool inventory")
                result = await session.call_tool("get_my_employee_profile", {})
                if result.isError:
                    raise RuntimeError(f"Profile retrieval failed: {result.content}")
                payload = result.structuredContent
                if not payload or payload.get("display_name") != "Synthetic Employee":
                    raise RuntimeError("Unexpected synthetic profile")
                if "private_salary" in json.dumps(payload):
                    raise RuntimeError("Sensitive column leaked")
                print("PASS own synthetic profile and approved response fields")
                result = await session.call_tool("list_my_requests", {})
                if result.isError:
                    raise RuntimeError("Request listing failed")
                arguments = {
                    "request_id": str(uuid4()),
                    "category": "IT",
                    "summary": "Synthetic integration test request",
                }
                result = await session.call_tool("create_support_request", arguments)
                if not operator:
                    if not result.isError:
                        raise RuntimeError("Reader unexpectedly invoked write tool")
                    print("PASS reader cannot invoke write tool")
                else:
                    if result.isError or not result.structuredContent.get("created"):
                        raise RuntimeError("Operator write failed")
                    result = await session.call_tool("create_support_request", arguments)
                    if result.isError or result.structuredContent.get("created"):
                        raise RuntimeError("Idempotency verification failed")
                    print("PASS operator write and idempotent retry")
    return {"protocol": "pass", "profile": "pass", "authorization": "pass"}


def acquire_token(tenant: str, client_id: str, api_id: str) -> str:
    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{tenant}"
    )
    flow = app.initiate_device_flow(scopes=[f"api://{api_id}/access_as_user"])
    if "user_code" not in flow:
        raise RuntimeError(f"Device sign-in unavailable: {flow.get('error')}")
    print(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(
            f"Sign-in failed: {result.get('error_description', result.get('error'))}"
        )
    return result["access_token"]


def main():
    parser = argparse.ArgumentParser(description="Exercise the MCP demo using the official MCP SDK")
    parser.add_argument("--url")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--operator", action="store_true")
    parser.add_argument("--state", default=".deployment/state.json")
    args = parser.parse_args()
    if args.local:
        token, url = None, args.url or "http://127.0.0.1:8000"
    else:
        state = json.loads(Path(args.state).read_text(encoding="utf-8-sig"))
        url = args.url or state["outputs"]["publicUrl"]["value"]
        token = os.environ.get("MCP_ACCESS_TOKEN") or acquire_token(
            state["tenantId"], state["clientAppId"], state["apiAppId"]
        )
    asyncio.run(exercise(url, token, operator=args.operator or args.local))


if __name__ == "__main__":
    main()
