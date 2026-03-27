#!/usr/bin/env python3
"""
Post-deployment health verification with automatic rollback.

Flow:
  1. For each target device, find the space for APP_NAME and poll containerinfo
     until health.Status is "healthy" or "unhealthy" (not "starting"), or timeout.
  2. If all devices are healthy → exit 0.
  3. If any device is unhealthy or timed out:
     - If a previous app version exists in Barbara:
         deploy it to ALL devices, then delete the failed version from Barbara.
     - If no previous version exists (first ever deploy):
         delete the workload from ALL devices, then delete the failed version from Barbara.
     - Exit 1 to fail the pipeline.
"""
import json
import os
import pathlib
import sys
import time
import urllib.error

from barbara_api import authorized_json_request, dump_http_error, request_keycloak_token, require_env


DEFAULT_TARGETS_FILE = "deployment-targets.json"
PAGE_SIZE = 100
DEFAULT_VERIFY_TIMEOUT = 300   # seconds
DEFAULT_VERIFY_INTERVAL = 30   # seconds

HEALTH_HEALTHY = "healthy"
HEALTH_UNHEALTHY = "unhealthy"
HEALTH_STARTING = "starting"


def normalize_name(value: str) -> str:
    return value.strip().casefold()


def get_access_token() -> str:
    token_response = request_keycloak_token()
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Keycloak token response does not contain access_token")
    return access_token


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
            "GET", "/api/v1/devices", access_token,
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
        group_list = payload.get("groupList", [])
        return group_list if isinstance(group_list, list) else []
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


def request_container_info_refresh(access_token: str, device_id: str, space_id: str) -> None:
    """Asks the device agent to report fresh container info via MQTT."""
    try:
        authorized_json_request(
            "POST",
            f"/api/v1/devices/{device_id}/workloads/{space_id}/info",
            access_token,
            payload={"action": "container"},
        )
    except urllib.error.HTTPError:
        pass


def _get_containerinfo(access_token: str, device_id: str, space_id: str) -> dict | None:
    """Returns the first container's inspect dict, or None if unavailable."""
    try:
        response = authorized_json_request(
            "GET",
            f"/api/v1/devices/{device_id}/workloads/{space_id}/containerinfo",
            access_token,
        )
        payload = response.get("response", response)
        containers = payload.get("info", []) if isinstance(payload, dict) else []
        return containers[0] if containers else None
    except urllib.error.HTTPError:
        return None


def get_container_health(access_token: str, device_id: str, space_id: str) -> str | None:
    """Returns health.Status string from the first container, or None if unavailable."""
    container = _get_containerinfo(access_token, device_id, space_id)
    if container is None:
        return None
    health = container.get("health")
    if isinstance(health, dict):
        return health.get("Status")
    return None


def get_container_image(access_token: str, device_id: str, space_id: str) -> str | None:
    """Returns the image reference of the running container, or None if unavailable."""
    container = _get_containerinfo(access_token, device_id, space_id)
    if container is None:
        return None
    for candidate in [
        (container.get("Config") or {}).get("Image"),
        (container.get("config") or {}).get("Image"),
        (container.get("config") or {}).get("image"),
        container.get("Image"),
        container.get("image"),
    ]:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def wait_for_new_container(access_token: str, devices_with_spaces: list[dict],
                           expected_image: str, timeout: int, interval: int) -> None:
    """
    Waits until all devices show the expected container image.
    Raises RuntimeError on timeout.
    """
    pending = {d["device_id"]: d for d in devices_with_spaces}
    deadline = time.monotonic() + timeout

    print(f"Waiting for new container image on all devices (timeout: {timeout}s, interval: {interval}s)...")
    while pending and time.monotonic() < deadline:
        for device_id, info in pending.items():
            request_container_info_refresh(access_token, device_id, info["space_id"])

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))

        resolved_ids = []
        for device_id, info in pending.items():
            device_name = info["device_name"]
            image = get_container_image(access_token, device_id, info["space_id"])
            if image is None:
                print(f"  {device_name}: no container running yet...")
            elif image == expected_image:
                print(f"  {device_name}: new container detected")
                resolved_ids.append(device_id)
            else:
                print(f"  {device_name}: old container still running ({image})")

        for device_id in resolved_ids:
            del pending[device_id]

    if pending:
        names = [info["device_name"] for info in pending.values()]
        raise RuntimeError(f"Timeout waiting for new container image on: {', '.join(names)}")


def find_application_by_name(access_token: str, app_name: str) -> dict:
    response = authorized_json_request(
        "GET", "/api/v1/applications/list", access_token,
        query={"filter": "docker", "search": app_name},
    )
    applications = response.get("response", [])
    if not isinstance(applications, list):
        raise RuntimeError("Unexpected Barbara API response for application list")
    expected = normalize_name(app_name)
    for application in applications:
        name = application.get("name", "")
        if isinstance(name, str) and normalize_name(name) == expected:
            return application
    raise RuntimeError(f"Barbara application not found: {app_name}")


def get_ordered_versions(access_token: str, application_id: str) -> list[dict]:
    """Returns all app versions sorted by creation date ascending."""
    response = authorized_json_request(
        "GET",
        f"/api/v1/applications/{application_id}/appversions",
        access_token,
        query={"from": "0", "size": "100", "filter": "all", "sortOrder": "asc", "sortColumn": "created"},
    )
    payload = response.get("response", {})
    versions = payload.get("appVersionList", []) if isinstance(payload, dict) else []
    if not isinstance(versions, list):
        raise RuntimeError("Unexpected Barbara API response for application versions")
    return versions


def find_version_by_name(versions: list[dict], version_name: str) -> dict | None:
    expected = normalize_name(version_name)
    for version in versions:
        name = version.get("name", "")
        if isinstance(name, str) and normalize_name(name) == expected:
            return version
    return None


def deploy_version_to_device(access_token: str, device_id: str, space_id: str,
                             application_id: str, app_version_id: str) -> None:
    authorized_json_request(
        "PUT",
        f"/api/v1/devices/{device_id}/user/workloads/{space_id}",
        access_token,
        payload={
            "applicationId": application_id,
            "appVersionId": app_version_id,
            "runDocker": True,
            "forcePull": True,
            "enableLogs": True,
        },
    )


def delete_workload_from_device(access_token: str, device_id: str, space_id: str) -> None:
    authorized_json_request(
        "DELETE",
        f"/api/v1/devices/{device_id}/workloads/{space_id}",
        access_token,
    )


def delete_app_version(access_token: str, application_id: str, app_version_id: str) -> None:
    authorized_json_request(
        "DELETE",
        f"/api/v1/applications/{application_id}/appversions/{app_version_id}",
        access_token,
    )


def poll_health(access_token: str, devices_with_spaces: list[dict], timeout: int, interval: int) -> dict[str, str]:
    """
    Polls health for all devices until all are determined (healthy/unhealthy) or timeout.
    Returns a dict: {device_id: health_status}
    where health_status is "healthy", "unhealthy", or "timeout".
    """
    pending = {d["device_id"]: d for d in devices_with_spaces}
    results: dict[str, str] = {}
    deadline = time.monotonic() + timeout

    while pending and time.monotonic() < deadline:
        for device_id, info in pending.items():
            request_container_info_refresh(access_token, device_id, info["space_id"])

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))

        resolved_ids = []
        for device_id, info in pending.items():
            health = get_container_health(access_token, device_id, info["space_id"])
            device_name = info["device_name"]
            if health is None:
                print(f"  {device_name}: container info not available yet, retrying...")
            elif health == HEALTH_STARTING:
                print(f"  {device_name}: starting...")
            else:
                print(f"  {device_name}: {health}")
                results[device_id] = health
                resolved_ids.append(device_id)

        for device_id in resolved_ids:
            del pending[device_id]

    for device_id, info in pending.items():
        print(f"  {info['device_name']}: timeout waiting for health status")
        results[device_id] = "timeout"

    return results


def rollback(access_token: str, devices_with_spaces: list[dict], application_id: str,
             failed_version_id: str, previous_version: dict | None) -> None:
    if previous_version is not None:
        previous_version_id = previous_version["_id"]
        previous_version_name = previous_version.get("name")
        print(f"\nRolling back to version '{previous_version_name}' on all devices...")
        for info in devices_with_spaces:
            device_id = info["device_id"]
            device_name = info["device_name"]
            space_id = info["space_id"]
            try:
                deploy_version_to_device(access_token, device_id, space_id, application_id, previous_version_id)
                print(f"  {device_name}: rollback deployed")
            except urllib.error.HTTPError as exc:
                print(f"  {device_name}: rollback failed — {dump_http_error(exc)}", file=sys.stderr)
    else:
        print("\nNo previous version found. Removing workloads from all devices...")
        for info in devices_with_spaces:
            device_id = info["device_id"]
            device_name = info["device_name"]
            space_id = info["space_id"]
            try:
                delete_workload_from_device(access_token, device_id, space_id)
                print(f"  {device_name}: workload removed")
            except urllib.error.HTTPError as exc:
                print(f"  {device_name}: workload removal failed — {dump_http_error(exc)}", file=sys.stderr)

    print("\nDeleting failed version from Barbara...")
    delete_retries = 60
    delete_delay = 5
    for attempt in range(delete_retries):
        try:
            delete_app_version(access_token, application_id, failed_version_id)
            print("  Failed version deleted from Barbara.")
            break
        except urllib.error.HTTPError as exc:
            error = dump_http_error(exc)
            if attempt < delete_retries - 1:
                print(f"  Delete attempt {attempt + 1}/{delete_retries} failed, retrying in {delete_delay}s: {error}", file=sys.stderr)
                time.sleep(delete_delay)
            else:
                print(f"  Could not delete failed version after {delete_retries} attempts: {error}", file=sys.stderr)


def main() -> int:
    app_name = require_env("APP_NAME")
    version_name = require_env("APP_VERSION_NAME")
    image_ref = os.environ.get("APP_VERSION_IMAGE_REF")
    timeout = int(os.environ.get("VERIFY_TIMEOUT_SECONDS", DEFAULT_VERIFY_TIMEOUT))
    interval = int(os.environ.get("VERIFY_INTERVAL_SECONDS", DEFAULT_VERIFY_INTERVAL))
    targets_path = pathlib.Path(DEFAULT_TARGETS_FILE)

    try:
        requested_devices, requested_groups = load_targets_file(targets_path)
        if not requested_devices and not requested_groups:
            print(json.dumps({"status": "skipped", "reason": "No deployment targets defined"}, indent=2))
            return 0

        access_token = get_access_token()
        devices = resolve_target_devices(access_token, requested_devices, requested_groups)

        # Find the space for APP_NAME on each device
        devices_with_spaces: list[dict] = []
        for device in devices:
            device_id = device.get("_id")
            device_name = device.get("deviceName") or ""
            if not isinstance(device_id, str):
                continue
            full_device = get_full_device(access_token, device_id)
            space = find_space_by_app_name(full_device, app_name)
            if space is None:
                print(f"WARNING: No space found for '{app_name}' on {device_name}, skipping.", file=sys.stderr)
                continue
            space_id = space.get("spaceId")
            if not space_id:
                print(f"WARNING: Space found but spaceId is missing on {device_name}, skipping.", file=sys.stderr)
                continue
            devices_with_spaces.append({
                "device_id": device_id,
                "device_name": device_name,
                "space_id": space_id,
            })

        if not devices_with_spaces:
            print(json.dumps({"status": "error", "reason": "No spaces found for the app on any device"}, indent=2), file=sys.stderr)
            return 1

        # Look up Barbara application and version info now (needed for rollback)
        application = find_application_by_name(access_token, app_name)
        application_id = application.get("_id")
        if not isinstance(application_id, str) or not application_id:
            raise RuntimeError("Barbara application response does not contain a valid _id")

        all_versions = get_ordered_versions(access_token, application_id)
        current_version = find_version_by_name(all_versions, version_name)
        if current_version is None:
            raise RuntimeError(f"Could not find version '{version_name}' in Barbara for application '{app_name}'")

        failed_version_id = current_version["_id"]
        current_version_index = all_versions.index(current_version)
        previous_version = all_versions[current_version_index - 1] if current_version_index > 0 else None

        # Wait for new container image before polling health
        if image_ref:
            wait_for_new_container(access_token, devices_with_spaces, image_ref, timeout, interval)

        # Poll health
        print(f"Verifying deployment of '{app_name}' version '{version_name}' "
              f"(timeout: {timeout}s, interval: {interval}s)...")
        health_results = poll_health(access_token, devices_with_spaces, timeout, interval)

        unhealthy = [
            info["device_name"]
            for info in devices_with_spaces
            if health_results.get(info["device_id"]) != HEALTH_HEALTHY
        ]

        if not unhealthy:
            print(json.dumps({
                "status": "ok",
                "version": version_name,
                "devices": [
                    {"deviceName": info["device_name"], "health": health_results[info["device_id"]]}
                    for info in devices_with_spaces
                ],
            }, indent=2))
            return 0

        # Rollback
        print(json.dumps({
            "status": "unhealthy",
            "version": version_name,
            "unhealthyDevices": unhealthy,
            "previousVersion": previous_version.get("name") if previous_version else None,
        }, indent=2), file=sys.stderr)

        rollback(access_token, devices_with_spaces, application_id, failed_version_id, previous_version)
        return 1

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
