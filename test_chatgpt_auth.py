"""Offline OAuth boundary and refresh checks. No real credentials or API calls."""
import asyncio
import json
import io
import os
import stat
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode, parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

import chatgpt_auth
import chatgpt_login


def credentials(**changes):
    return {
        "issuer": chatgpt_login.ISSUER, "access_token": "old-access", "refresh_token": "old-refresh",
        "subject": "account-a", "client_id": "issued-client", "scope": chatgpt_login.SCOPES,
        "token_type": "Bearer", "expires_at": time.time() - 10, **changes,
    }


def test_callback_and_pkce():
    challenge = chatgpt_login.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
    assert challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    callback = "/auth/callback?" + urlencode({"state": "pending", "code": "code", "client_id": "issued-client"})
    assert chatgpt_login.callback_data(callback, "pending")["client_id"] == "issued-client"
    assert chatgpt_login.callback_data("/auth/callback?state=pending&code=c", "pending", "issued-client")["code"] == "c"
    assert "error" in chatgpt_login.callback_data("/auth/callback?state=pending&error=access_denied", "pending")
    for target in (callback.replace("pending", "other"), callback + "&code=duplicate",
                   callback.replace("issued-client", "dynamic_agent_client"),
                   callback.replace("/auth/callback", "/other"), callback + "&extra=" + "x" * 16384):
        try:
            chatgpt_login.callback_data(target, "pending")
        except ValueError:
            pass
        else:
            raise AssertionError("Bad callback accepted")
    try:
        chatgpt_login.callback_data(callback, "pending", "another-client")
    except ValueError:
        pass
    else:
        raise AssertionError("Client identity changed")


def test_verified_identity():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    claims = {"iss": chatgpt_login.ISSUER, "aud": "issued-client", "sub": "account-a",
              "nonce": "pending", "exp": now + 600, "iat": now}
    def token_response(**changes):
        value = {"access_token": "access", "refresh_token": "refresh", "scope": chatgpt_login.SCOPES,
                 "token_type": "Bearer", "expires_in": 3600,
                 "id_token": jwt.encode(claims, key, algorithm="RS256")}
        return {**value, **changes}
    with patch.object(jwt.PyJWKClient, "get_signing_key_from_jwt", return_value=SimpleNamespace(key=key.public_key())):
        assert chatgpt_login.verify_tokens(token_response(), "issued-client", "pending")["sub"] == "account-a"
        for tokens, client, nonce, subject in (
            (token_response(), "other-client", "pending", None),
            (token_response(), "issued-client", "other-nonce", None),
            (token_response(), "issued-client", "pending", "other-account"),
            (token_response(scope="openid"), "issued-client", "pending", None),
            (token_response(expires_in=True), "issued-client", "pending", None),
            (token_response(id_token=jwt.encode({**claims, "exp": now - 60}, key, algorithm="RS256")), "issued-client", "pending", None),
        ):
            try:
                chatgpt_login.verify_tokens(tokens, client, nonce, subject)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Invalid identity or grant accepted")


def test_login_preserves_registration_before_exchange():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        opened = []
        tokens = credentials(expires_in=3600, id_token="verified-separately")
        class Server:
            server_port = 43210
            def __init__(self, address, handler):
                assert address == ("127.0.0.1", 0)
                self.handler = handler
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def handle_request(self):
                fields = parse_qs(urlsplit(opened[-1]).query)
                handler = self.handler.__new__(self.handler)
                handler.path = "/auth/callback?" + urlencode({"state": fields["state"][0], "code": "code",
                                                              "client_id": "issued-client"})
                handler.send_response = lambda status: None
                handler.send_header = lambda *args: None
                handler.end_headers = lambda: None
                handler.wfile = io.BytesIO()
                handler.do_GET()
        def exchange(fields):
            registration = json.loads((root / "registration.json").read_text())
            query = parse_qs(urlsplit(opened[-1]).query)
            assert registration["client_id"] == fields["client_id"] == "issued-client"
            assert fields["redirect_uri"] == query["redirect_uri"][0]
            assert chatgpt_login.pkce_challenge(fields["code_verifier"]) == query["code_challenge"][0]
            return tokens
        with patch.object(chatgpt_login, "HTTPServer", Server), \
                patch.object(chatgpt_login.webbrowser, "open", side_effect=lambda url: opened.append(url)), \
                patch.object(chatgpt_login, "token_request", side_effect=exchange), \
                patch.object(chatgpt_login, "verify_tokens", return_value={"sub": "account-a"}) as verify, \
                patch("builtins.print"):
            chatgpt_login.login(root)
            chatgpt_login.login(root)
        first, second = [parse_qs(urlsplit(url).query) for url in opened]
        assert first["client_id"] == ["dynamic_agent_client"]
        assert second["client_id"] == ["issued-client"]
        assert first["agent_name_hint"] == ["AntiSlop"] and "agent_name_hint" not in second
        assert first["ext_agent_host_id"] == second["ext_agent_host_id"]
        assert first["state"] != second["state"] and first["nonce"] != second["nonce"]
        assert verify.call_args.args[3] == "account-a"
        assert json.loads((root / "credentials.json").read_text())["subject"] == "account-a"


async def test_rotation_concurrency_and_status():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "credentials.json"
        chatgpt_login.save_json(path, credentials())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        calls = []
        async def handler(request):
            calls.append(request)
            form = parse_qs(request.content.decode())
            assert request.url == chatgpt_login.ISSUER + "/api/accounts/oauth/token"
            assert form["refresh_token"] == ["old-refresh"] and form["client_id"] == ["issued-client"]
            assert form["resource"] == [chatgpt_login.RESOURCE]
            assert "scope" not in form and "Authorization" not in request.headers
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh",
                                           "token_type": "Bearer", "expires_in": 3600})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = chatgpt_auth.ChatGPTAuth(path, client=client)
            other = chatgpt_auth.ChatGPTAuth(path, client=client)
            assert await asyncio.gather(auth.access_token(), other.access_token()) == ["new-access"] * 2
        assert len(calls) == 1
        saved = json.loads(path.read_text())
        assert saved["refresh_token"] == "new-refresh" and saved["subject"] == "account-a"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with patch.object(chatgpt_auth, "DIRECTORY", root), patch.dict(os.environ, {"OPENAI_API_KEY": "synthid-only"}):
            status = chatgpt_auth.status()
            assert status["configured"] and "token" not in json.dumps(status)
            path.unlink()
            assert not chatgpt_auth.status()["configured"]


async def test_cancel_saves_rotated_token():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "credentials.json"
        chatgpt_login.save_json(path, credentials())
        started, proceed = asyncio.Event(), asyncio.Event()
        async def handler(request):
            started.set()
            await proceed.wait()
            return httpx.Response(200, json={"access_token": "next-access", "refresh_token": "next-refresh",
                                           "token_type": "Bearer", "expires_in": 3600})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = chatgpt_auth.ChatGPTAuth(path, client=client)
            task = asyncio.create_task(auth.access_token())
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            proceed.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Cancellation was swallowed")
        assert json.loads(path.read_text())["refresh_token"] == "next-refresh"


async def test_refresh_failure_preserves_credentials():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "credentials.json"
        chatgpt_login.save_json(path, credentials())
        before = path.read_bytes()
        for code, payload in [(401, {"error": "private-secret"}), (200, {"access_token": "private-secret"}),
                              (200, {"access_token": "private-secret", "refresh_token": "new", "token_type": "Bearer", "expires_in": True})]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(code, json=payload))) as client:
                auth = chatgpt_auth.ChatGPTAuth(path, client=client)
                try:
                    await auth.access_token()
                except chatgpt_auth.AuthError as error:
                    assert "private-secret" not in str(error)
                else:
                    raise AssertionError("Bad refresh accepted")
            assert path.read_bytes() == before


if __name__ == "__main__":
    test_callback_and_pkce()
    test_verified_identity()
    test_login_preserves_registration_before_exchange()
    asyncio.run(test_rotation_concurrency_and_status())
    asyncio.run(test_cancel_saves_rotated_token())
    asyncio.run(test_refresh_failure_preserves_credentials())
    print("OAuth callback, identity, rotation and cancellation tests passed")
