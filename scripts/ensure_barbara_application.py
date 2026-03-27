#!/usr/bin/env python3
import json
import os
import sys
import urllib.error

from barbara_api import authorized_json_request, authorized_multipart_request, dump_http_error, request_keycloak_token, require_env


def get_env_with_default(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def get_target_application_payload() -> dict[str, str | bool]:
    return {
        "name": require_env("APP_NAME"),
        "developer": get_env_with_default("APP_DEVELOPER", "Barbara"),
        "longDescription": require_env("APP_LONG_DESCRIPTION"),
        "shortDescription": require_env("APP_SHORT_DESCRIPTION"),
        "docker": True,
    }


def normalize_name(value: str) -> str:
    return value.strip().casefold()


def list_candidate_applications(access_token: str, app_name: str) -> list[dict]:
    response = authorized_json_request(
        "GET",
        "/api/v1/applications/list",
        access_token,
        query={
            "filter": "docker",
            "search": app_name,
        },
    )
    applications = response.get("response", [])
    if not isinstance(applications, list):
        raise RuntimeError("Unexpected Barbara API response for application list")
    return applications


def find_exact_application_by_name(applications: list[dict], app_name: str) -> dict | None:
    expected = normalize_name(app_name)
    for application in applications:
        current_name = application.get("name", "")
        if isinstance(current_name, str) and normalize_name(current_name) == expected:
            return application
    return None


def create_application(access_token: str, payload: dict[str, str | bool]) -> dict:
    response = authorized_multipart_request("POST", "/api/v1/applications", access_token, payload)
    application = response.get("response", {}).get("application")
    if not isinstance(application, dict):
        raise RuntimeError("Unexpected Barbara API response for application creation")
    return application


def main() -> int:
    payload = get_target_application_payload()
    app_name = str(payload["name"])

    try:
        token_response = request_keycloak_token()
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Keycloak token response does not contain access_token")

        applications = list_candidate_applications(access_token, app_name)
        existing_application = find_exact_application_by_name(applications, app_name)
        if existing_application is not None:
            print(
                json.dumps(
                    {
                        "status": "exists",
                        "application": {
                            "_id": existing_application.get("_id"),
                            "name": existing_application.get("name"),
                            "apptype": existing_application.get("apptype"),
                            "lastVersion": existing_application.get("lastVersion"),
                        },
                    },
                    indent=2,
                )
            )
            return 0

        created_application = create_application(access_token, payload)
        print(
            json.dumps(
                {
                    "status": "created",
                    "application": {
                        "_id": created_application.get("_id"),
                        "name": created_application.get("name"),
                        "developer": created_application.get("developer"),
                        "docker": created_application.get("docker"),
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
