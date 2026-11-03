import json
from pathlib import Path
from xml.etree import ElementTree


def policy():
    text = (Path(__file__).parents[1] / "infra" / "apim-policy.xml").read_text()
    text = text.replace("__TENANT_ID__", "tenant").replace("__API_CLIENT_ID__", "audience")
    return ElementTree.fromstring(
        text.replace("__GATEWAY_URL__", "https://gateway.example").replace(
            "__BACKEND_URL__", "https://backend.example"
        )
    )


def test_gateway_identity_policy():
    jwt = policy().find(".//validate-jwt")
    assert jwt.attrib["require-signed-tokens"] == "true"
    assert jwt.attrib["require-expiration-time"] == "true"
    assert jwt.find("audiences/audience").text == "audience"
    claims = {node.attrib["name"] for node in jwt.findall("required-claims/claim")}
    assert claims == {"tid", "scp", "roles"}
    assert policy().find(".//rate-limit-by-key").attrib["calls"] == "30"


def test_gateway_metadata_and_streaming():
    root = policy()
    metadata = json.loads(root.find("inbound/choose/when/return-response/set-body").text)
    assert metadata["resource"] == "https://gateway.example/mcp"
    assert metadata["scopes_supported"] == ["api://audience/access_as_user"]
    assert root.find("backend/base") is not None
    assert root.find("backend/forward-request") is None
    assert root.find(".//set-backend-service").attrib["base-url"] == "https://backend.example"
    assert root.find(".//set-header[@name='Authorization']") is None
    challenge = root.find("on-error/choose/when/return-response/set-header")
    assert "https://gateway.example/.well-known/oauth-protected-resource" in challenge[0].text


def test_native_mcp_routing_is_not_overridden():
    root = policy()
    routes = root.findall("inbound/choose")[1]
    health = routes.find("when")
    assert 'OriginalUrl.Path.TrimEnd((char)47) == "/health"' in health.attrib["condition"]
    assert health.find("rewrite-uri").attrib["template"] == "/health"
    assert routes.find("otherwise") is None
    assert len(root.findall(".//rewrite-uri")) == 1
    assert root.find(".//set-header[@name='Host']") is None
