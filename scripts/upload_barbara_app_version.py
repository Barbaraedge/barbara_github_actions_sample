#!/usr/bin/env python3
import json
import mimetypes
import os
import pathlib
import sys
import urllib.error

from barbara_api import authorized_json_request, authorized_multipart_request, dump_http_error, request_keycloak_token, require_env


def normalize_name(value: str) -> str:
    return value.strip().casefold()


def split_csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_access_token() -> str:
    token_response = request_keycloak_token()
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Keycloak token response does not contain access_token")
    return access_token


def find_application_by_name(access_token: str, app_name: str) -> dict:
    response = authorized_json_request(
        "GET",
        "/api/v1/applications/list",
        access_token,
        query={"filter": "docker", "search": app_name},
    )
    applications = response.get("response", [])
    if not isinstance(applications, list):
        raise RuntimeError("Unexpected Barbara API response for application list")

    expected = normalize_name(app_name)
    for application in applications:
        current_name = application.get("name", "")
        if isinstance(current_name, str) and normalize_name(current_name) == expected:
            return application

    raise RuntimeError(f"Barbara application not found: {app_name}")


def version_exists(access_token: str, application_id: str, version_name: str) -> bool:
    response = authorized_json_request(
        "GET",
        f"/api/v1/applications/{application_id}/appversions",
        access_token,
        query={
            "from": "0",
            "size": "100",
            "filter": "all",
            "search": version_name,
            "sortOrder": "asc",
            "sortColumn": "name",
        },
    )
    payload = response.get("response", {})
    versions = payload.get("appVersionList", []) if isinstance(payload, dict) else []
    if not isinstance(versions, list):
        raise RuntimeError("Unexpected Barbara API response for application versions")

    expected = normalize_name(version_name)
    return any(isinstance(version.get("name"), str) and normalize_name(version["name"]) == expected for version in versions)


def guess_content_type(file_path: pathlib.Path) -> str:
    content_type, _ = mimetypes.guess_type(file_path.name)
    return content_type or "application/octet-stream"


def upload_app_version(access_token: str, application_id: str, version_name: str, artifact_path: pathlib.Path, architectures: list[str], release_notes: list[str]) -> dict:
    file_bytes = artifact_path.read_bytes()
    response = authorized_multipart_request(
        "POST",
        f"/api/v1/applications/{application_id}/appversions",
        access_token,
        fields={
            "name": version_name,
            "architectures[]": architectures,
            "releaseNotes[]": release_notes,
        },
        files={
            "url": (artifact_path.name, file_bytes, guess_content_type(artifact_path)),
        },
    )
    payload = response.get("response")
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Barbara API response for application version upload")
    return payload


def main() -> int:
    app_name = require_env("APP_NAME")
    version_name = require_env("APP_VERSION_NAME")
    artifact_path = pathlib.Path(require_env("APP_VERSION_ARTIFACT_PATH"))
    architectures = split_csv_env("APP_VERSION_ARCHITECTURES", ["x86_64"])
    release_notes = split_csv_env("APP_VERSION_RELEASE_NOTES", [f"Release {version_name}"])

    if not artifact_path.exists():
        raise RuntimeError(f"Artifact file not found: {artifact_path}")

    try:
        access_token = get_access_token()
        application = find_application_by_name(access_token, app_name)
        application_id = application.get("_id")
        if not isinstance(application_id, str) or not application_id:
            raise RuntimeError("Barbara application response does not contain a valid _id")

        if version_exists(access_token, application_id, version_name):
            print(
                json.dumps(
                    {
                        "status": "exists",
                        "application": {"_id": application_id, "name": application.get("name")},
                        "version": version_name,
                    },
                    indent=2,
                )
            )
            return 0

        created_version = upload_app_version(access_token, application_id, version_name, artifact_path, architectures, release_notes)
        print(
            json.dumps(
                {
                    "status": "created",
                    "application": {"_id": application_id, "name": application.get("name")},
                    "version": {
                        "_id": created_version.get("_id"),
                        "name": created_version.get("name"),
                        "url": created_version.get("url"),
                    },
                },
                indent=2,
            )
        )
        return 0
    except urllib.error.HTTPError as exc:
        print(json.dumps(dump_http_error(exc), indent=2), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
