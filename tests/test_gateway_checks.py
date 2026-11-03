import httpx
import pytest

from app.gateway_checks import check_gateway


@pytest.mark.parametrize(
    ("advertised_url", "different_metadata", "error"),
    [
        ("https://gateway.example/.well-known/oauth-protected-resource", False, None),
        ("https://gateway.example//.well-known/oauth-protected-resource", False, None),
        ("https://other.example/.well-known/oauth-protected-resource", False, "challenge"),
        ("https://gateway.example/unrelated", False, "challenge"),
        ("https://gateway.example/.well-known/oauth-protected-resource", True, "differs"),
        (None, False, "challenge"),
    ],
)
async def test_gateway_checks_metadata(monkeypatch, advertised_url, different_metadata, error):
    metadata = {
        "resource": "https://gateway.example/mcp",
        "scopes_supported": ["api://audience/access_as_user"],
    }
    discovery_calls = 0

    def handle(request):
        nonlocal discovery_calls
        assert request.url.host == "gateway.example"
        if request.url.path == "/mcp":
            headers = {}
            if advertised_url:
                headers["WWW-Authenticate"] = f'Bearer resource_metadata="{advertised_url}"'
            return httpx.Response(401, headers=headers)
        discovery_calls += 1
        document = {} if different_metadata and discovery_calls > 1 else metadata
        return httpx.Response(200, json=document)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    if error:
        with pytest.raises(RuntimeError, match=error):
            await check_gateway("https://gateway.example", "audience", None, False)
    else:
        await check_gateway("https://gateway.example", "audience", None, False)
        assert discovery_calls == 3
