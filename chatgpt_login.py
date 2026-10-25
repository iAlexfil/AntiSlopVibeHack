"""Obtain local ChatGPT plan OAuth credentials for AntiSlop."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + DIRECT_SCOPE
DIRECTORY = Path(__file__).resolve().parent / ".antislop-auth"


def save_json(path, value):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".oauth-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")


def callback_data(target, expected_state, existing_client_id=None):
    parsed = urlsplit(target)
    if parsed.path != "/auth/callback" or len(target) > 16384:
        raise ValueError("Unexpected callback path")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()):
        raise ValueError("Repeated callback parameter")
    values = {key: items[0] for key, items in query.items()}
    if not secrets.compare_digest(values.get("state", ""), expected_state):
        raise ValueError("Incorrect OAuth state")
    if "error" in values:
        return {"error": "Вход отклонён или не выполнен. Сохранённая сессия не изменена."}
    client_id = values.get("client_id") or existing_client_id
    if not client_id or client_id == "dynamic_agent_client" or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", client_id):
        raise ValueError("No valid issued client ID")
    if existing_client_id and client_id != existing_client_id:
        raise ValueError("The returned client ID changed")
    if not values.get("code"):
        raise ValueError("No authorization code")
    return {"code": values["code"], "client_id": client_id}


def token_request(fields):
    request = Request(
        ISSUER + "/api/accounts/oauth/token",
        data=urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(262145)
    except HTTPError as error:
        raise RuntimeError(f"Обмен кода OpenAI не выполнен (HTTP {error.code}). Запустите вход снова.") from None
    if len(raw) > 262144:
        raise RuntimeError("OpenAI token response is too large")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise RuntimeError("OpenAI token response is not an object")
    return result


def verify_tokens(tokens, client_id, nonce, expected_subject=None):
    import jwt

    for field in ("access_token", "refresh_token", "id_token", "scope"):
        if not isinstance(tokens.get(field), str) or not tokens[field]:
            raise RuntimeError(f"OpenAI did not return {field}. Credentials were not saved.")
    if not isinstance(tokens.get("token_type"), str) or tokens["token_type"].lower() != "bearer":
        raise RuntimeError("Unexpected OAuth token type")
    jwks = jwt.PyJWKClient(ISSUER + "/.well-known/jwks.json", timeout=20)
    try:
        signing_key = jwks.get_signing_key_from_jwt(tokens["id_token"])
        claims = jwt.decode(
            tokens["id_token"],
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=ISSUER,
            leeway=5,
            options={"require": ["iss", "aud", "sub", "exp", "iat", "nonce"]},
        )
    except jwt.PyJWTError:
        raise RuntimeError("ID-токен OpenAI не прошёл проверку. Сессия не сохранена.") from None
    if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce):
        raise RuntimeError("ID token nonce does not match")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise RuntimeError("ID token has no account identity")
    if expected_subject and claims["sub"] != expected_subject:
        raise RuntimeError("Выбран другой аккаунт ChatGPT. Сохранённая сессия не заменена.")
    granted = set(tokens["scope"].split())
    if not {DIRECT_SCOPE, "offline_access", "resource.invoke"}.issubset(granted):
        raise RuntimeError("Разрешение использовать план ChatGPT не получено. Сессия не сохранена.")
    if type(tokens.get("expires_in")) is not int or tokens["expires_in"] <= 0:
        raise RuntimeError("OpenAI did not return a valid token lifetime")
    return claims


def login(directory):
    registration_path = directory / "registration.json"
    credentials_path = directory / "credentials.json"
    registration = json.loads(registration_path.read_text()) if registration_path.exists() else {
        "ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4())
    }
    save_json(registration_path, registration)
    existing_client_id = registration.get("client_id")
    if credentials_path.exists() and not existing_client_id:
        raise RuntimeError("Existing credentials have no saved client registration. Refusing to overwrite them.")
    expected_subject = registration.get("subject")
    if credentials_path.exists():
        previous = json.loads(credentials_path.read_text())
        if previous.get("client_id") != existing_client_id:
            raise RuntimeError("Existing credentials and registration do not match")
        expected_subject = previous["subject"]
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    result = []

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass  # Callback URLs contain authorization codes.

        def do_GET(self):
            try:
                data = callback_data(self.path, state, existing_client_id)
            except (ValueError, UnicodeError):
                status, body = 400, b"Invalid callback. Return to the original sign-in page."
            else:
                result.append(data)
                status, body = 200, b"Callback received. You can close this tab; check your terminal for the login result."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
        fields = {
            "client_id": existing_client_id or "dynamic_agent_client",
            "ext_agent_host_id": registration["ext_agent_host_id"],
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": pkce_challenge(verifier),
        }
        if not existing_client_id:
            fields["agent_name_hint"] = "AntiSlop"
        authorization_url = ISSUER + "/api/accounts/authorize?" + urlencode(fields)
        print("Войдите в браузере и разрешите использование плана ChatGPT для AntiSlop.")
        print("Если браузер не открылся, откройте эту ссылку локально:\n" + authorization_url)
        webbrowser.open(authorization_url)
        deadline = time.monotonic() + 600
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise RuntimeError("Время входа истекло через 10 минут. Запустите скрипт снова.")
    callback = result[0]
    if "error" in callback:
        raise RuntimeError(callback["error"])
    registration["client_id"] = callback["client_id"]
    save_json(registration_path, registration)
    tokens = token_request({
        "grant_type": "authorization_code",
        "client_id": callback["client_id"],
        "code": callback["code"],
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "resource": RESOURCE,
    })
    received_at = time.time()
    claims = verify_tokens(tokens, callback["client_id"], nonce, expected_subject)
    credentials = {
        **tokens,
        "issuer": ISSUER,
        "subject": claims["sub"],
        "client_id": callback["client_id"],
        "ext_agent_host_id": registration["ext_agent_host_id"],
        "scopes": tokens["scope"].split(),
        "saved_at": received_at,
        "expires_at": received_at + tokens["expires_in"],
    }
    save_json(credentials_path, credentials)
    registration["subject"] = claims["sub"]
    save_json(registration_path, registration)
    print(f"Вход подтверждён. Учётные данные сохранены: {credentials_path}")
    print("Доступ к лимитам плана разрешён. Запрос анализа не отправлялся.")
    print("Не публикуйте содержимое хранилища авторизации.")


def main():
    try:
        import jwt  # noqa: F401
    except ImportError:
        raise SystemExit("Install the dependency first: python -m pip install 'PyJWT[crypto]>=2.10,<3'")
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    DIRECTORY.chmod(0o700)
    descriptor = os.open(DIRECTORY / ".login.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Другой процесс входа уже использует это хранилище.")
        try:
            login(DIRECTORY)
        except KeyboardInterrupt:
            raise SystemExit("Вход отменён.")
        except RuntimeError as error:
            raise SystemExit(str(error)) from None
        except (OSError, ValueError, KeyError, TypeError, URLError):
            # Avoid printing server responses, callback URLs, or token-bearing exceptions.
            raise SystemExit("Не удалось войти или доступ отклонён. Сохранённая сессия не изменена; запустите скрипт снова.") from None


if __name__ == "__main__":
    main()
