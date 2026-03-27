#!/usr/bin/env python3
import json
import pathlib
import sys
import urllib.error

from barbara_api import authorized_json_request, dump_http_error, request_keycloak_token, require_env


DEFAULT_TARGETS_FILE = "deployment-targets.json"
PAGE_SIZE = 100


def normalize_name(value: str) -> str:
    return value.strip().casefold()


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


def find_version_by_name(access_token: str, application_id: str, version_name: str) -> dict:
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
    for version in versions:
        current_name = version.get("name", "")
        if isinstance(current_name, str) and normalize_name(current_name) == expected:
            return version

    raise RuntimeError(f"Barbara app version not found: {version_name}")


def load_targets_file(path: pathlib.Path) -> tuple[list[str], list[str]]:
    if not path.exists():
        raise RuntimeError(f"Deployment targets file not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Deployment targets file must contain a JSON object.")

    def require_string_list(field_name: str) -> list[str]:
        raw = payload.get(field_name, [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise RuntimeError(f'The "{field_name}" field must be an array.')
        return [item.strip() for item in raw if isinstance(item, str) and item.strip()]

    return require_string_list("devices"), require_string_list("groups")


def list_all_devices(access_token: str) -> list[dict]:
    devices: list[dict] = []
    from_index = 0

    while True:
        response = authorized_json_request(
            "GET",
            "/api/v1/devices",
            access_token,
            query={"from": str(from_index), "size": str(PAGE_SIZE)},
        )
        payload = response.get("response", response)
        if isinstance(payload, dict):
            current_page = payload.get("deviceList") or payload.get("devicesCustom") or []
            total = payload.get("total")
        elif isinstance(payload, list):
            current_page = payload
            total = None
        else:
            raise RuntimeError("Unexpected Barbara API response for device collection")

        devices.extend(current_page)
        if not current_page or len(current_page) < PAGE_SIZE:
            break
        if total is not None and len(devices) >= total:
            break
        from_index += len(current_page)

    return devices


def list_all_groups(access_token: str) -> list[dict]:
    response = authorized_json_request("GET", "/api/v1/groups", access_token)
    payload = response.get("response", response)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("groupList", [])
    raise RuntimeError("Unexpected Barbara API response for group list")


def resolve_target_devices(access_token: str, requested_devices: list[str], requested_groups: list[str]) -> list[dict]:
    all_devices = list_all_devices(access_token)
    devices_by_name = {normalize_name(d.get("deviceName", "")): d for d in all_devices if d.get("deviceName")}
    devices_by_id = {d.get("_id"): d for d in all_devices if d.get("_id")}

    missing_devices = [name for name in requested_devices if normalize_name(name) not in devices_by_name]

    resolved: list[dict] = [devices_by_name[normalize_name(name)] for name in requested_devices if normalize_name(name) in devices_by_name]

    if requested_groups:
        all_groups = list_all_groups(access_token)
        groups_by_name = {normalize_name(g.get("name", "")): g for g in all_groups if g.get("name")}
        missing_groups = [name for name in requested_groups if normalize_name(name) not in groups_by_name]

        if missing_devices or missing_groups:
            raise RuntimeError(json.dumps({"missingDevices": missing_devices, "missingGroups": missing_groups}))

        for group_name in requested_groups:
            group = groups_by_name[normalize_name(group_name)]
            for device_id in group.get("devices", []):
                device = devices_by_id.get(device_id)
                if device:
                    resolved.append(device)
    elif missing_devices:
        raise RuntimeError(json.dumps({"missingDevices": missing_devices, "missingGroups": []}))

    seen_ids: set[str] = set()
    unique: list[dict] = []
    for device in resolved:
        device_id = device.get("_id")
        if device_id and device_id not in seen_ids:
            seen_ids.add(device_id)
            unique.append(device)
    return unique


def get_full_device(access_token: str, device_id: str) -> dict:
    response = authorized_json_request("GET", f"/api/v1/devices/{device_id}", access_token)
    payload = response.get("response", response)
    if isinstance(payload, dict):
        return payload
    raise RuntimeError(f"Unexpected Barbara API response for device {device_id}")


def find_space_by_app_name(device: dict, app_name: str) -> dict | None:
    spaces = device.get("spaces", [])
    if not isinstance(spaces, list):
        return None
    expected = normalize_name(app_name)
    for space in spaces:
        current = space.get("current", {})
        if not isinstance(current, dict):
            continue
        application_id = current.get("applicationId")
        if isinstance(application_id, dict):
            name = application_id.get("name", "")
            if isinstance(name, str) and normalize_name(name) == expected:
                return space
    return None


def _workload_payload(application_id: str, app_version_id: str) -> dict:
    return {
        "applicationId": application_id,
        "appVersionId": app_version_id,
        "runDocker": True,
        "forcePull": True,
        "enableLogs": True,
    }


def create_workload_on_device(access_token: str, device_id: str, application_id: str, app_version_id: str) -> dict:
    return authorized_json_request(
        "POST",
        f"/api/v1/devices/{device_id}/user/workloads",
        access_token,
        payload=_workload_payload(application_id, app_version_id),
    )


def update_workload_on_device(access_token: str, device_id: str, space_id: str, application_id: str, app_version_id: str) -> dict:
    return authorized_json_request(
        "PUT",
        f"/api/v1/devices/{device_id}/user/workloads/{space_id}",
        access_token,
        payload=_workload_payload(application_id, app_version_id),
    )


def main() -> int:
    app_name = require_env("APP_NAME")
    version_name = require_env("APP_VERSION_NAME")
    targets_path = pathlib.Path(DEFAULT_TARGETS_FILE)

    try:
        requested_devices, requested_groups = load_targets_file(targets_path)

        if not requested_devices and not requested_groups:
            print(json.dumps({"status": "skipped", "reason": "No deployment targets defined"}, indent=2))
            return 0

        access_token = get_access_token()

        application = find_application_by_name(access_token, app_name)
        application_id = application.get("_id")
        if not isinstance(application_id, str) or not application_id:
            raise RuntimeError("Barbara application response does not contain a valid _id")

        version = find_version_by_name(access_token, application_id, version_name)
        app_version_id = version.get("_id")
        if not isinstance(app_version_id, str) or not app_version_id:
            raise RuntimeError("Barbara app version response does not contain a valid _id")

        devices = resolve_target_devices(access_token, requested_devices, requested_groups)

        results = []
        failed_devices = []
        for device in devices:
            device_id = device.get("_id")
            device_name = device.get("deviceName") or ""
            if not isinstance(device_id, str):
                continue
            try:
                full_device = get_full_device(access_token, device_id)
                existing_space = find_space_by_app_name(full_device, app_name)
                space_id = existing_space.get("spaceId") if existing_space is not None else None
                if isinstance(space_id, str):
                    update_workload_on_device(access_token, device_id, space_id, application_id, app_version_id)
                    results.append({"deviceId": device_id, "deviceName": device_name, "status": "updated", "spaceId": space_id})
                else:
                    create_workload_on_device(access_token, device_id, application_id, app_version_id)
                    results.append({"deviceId": device_id, "deviceName": device_name, "status": "created"})
            except urllib.error.HTTPError as exc:
                error_info = dump_http_error(exc)
                results.append({"deviceId": device_id, "deviceName": device_name, "status": "error", "error": error_info})
                failed_devices.append(device_name)

        summary = {
            "status": "error" if failed_devices else "ok",
            "application": {"_id": application_id, "name": application.get("name")},
            "version": {"_id": app_version_id, "name": version.get("name")},
            "devices": results,
        }
        print(json.dumps(summary, indent=2))

        if failed_devices:
            print(json.dumps({"failedDevices": failed_devices}, indent=2), file=sys.stderr)
            return 1

        return 0
    except urllib.error.HTTPError as exc:
        print(json.dumps(dump_http_error(exc), indent=2), file=sys.stderr)
        return 1
    except Exception as exc:
        error_message = str(exc)
        try:
            parsed = json.loads(error_message)
            print(json.dumps({"error": parsed}, indent=2), file=sys.stderr)
        except json.JSONDecodeError:
            print(json.dumps({"error": error_message}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
