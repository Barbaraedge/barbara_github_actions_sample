#!/usr/bin/env python3
import json
import os
import uuid
import urllib.error
import urllib.parse
import urllib.request


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def build_keycloak_token_url() -> str:
    auth_url = require_env("BARBARA_AUTH_URL").rstrip("/")
    realm = require_env("BARBARA_KEYCLOAK_REALM")
    return f"{auth_url}/realms/{realm}/protocol/openid-connect/token"


def request_keycloak_token() -> dict:
    payload = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": require_env("BARBARA_KEYCLOAK_CLIENT_ID"),
            "client_secret": require_env("BARBARA_KEYCLOAK_CLIENT_SECRET"),
            "username": require_env("BARBARA_KEYCLOAK_USER_EMAIL"),
            "password": require_env("BARBARA_KEYCLOAK_USER_PASSWORD"),
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        build_keycloak_token_url(),
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def build_api_url(path: str, query: dict[str, str] | None = None) -> str:
    base_url = require_env("BARBARA_API_URL").rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    if not query:
        return f"{base_url}{normalized_path}"
    return f"{base_url}{normalized_path}?{urllib.parse.urlencode(query)}"


def authorized_json_request(method: str, path: str, access_token: str, query: dict[str, str] | None = None, payload: dict | None = None) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {access_token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        build_api_url(path, query=query),
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def encode_multipart_form_data(fields: dict[str, object], files: dict[str, tuple[str, bytes, str]] | None = None) -> tuple[bytes, str]:
    boundary = f"----BarbaraBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            normalized = str(item).lower() if isinstance(item, bool) else str(item)
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                    normalized.encode("utf-8"),
                    b"\r\n",
                ]
            )

    if files:
        for field_name, (filename, content, content_type) in files.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8"),
                    f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                    content,
                    b"\r\n",
                ]
            )

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def authorized_multipart_request(method: str, path: str, access_token: str, fields: dict[str, object], files: dict[str, tuple[str, bytes, str]] | None = None) -> dict:
    data, content_type = encode_multipart_form_data(fields, files=files)
    request = urllib.request.Request(
        build_api_url(path),
        data=data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": content_type,
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def dump_http_error(exc: urllib.error.HTTPError) -> dict:
    body = exc.read().decode("utf-8", errors="replace")
    try:
        parsed_body = json.loads(body)
    except json.JSONDecodeError:
        parsed_body = body
    return {
        "status": exc.code,
        "reason": exc.reason,
        "body": parsed_body,
    }
