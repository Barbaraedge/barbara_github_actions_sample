#!/usr/bin/env python3
import json
import pathlib
import sys
import urllib.error

from barbara_api import authorized_json_request, dump_http_error, request_keycloak_token


DEFAULT_TARGETS_FILE = "deployment-targets.json"
DEFAULT_RESOLVED_TARGETS_FILE = "dist/resolved-deployment-targets.json"
PAGE_SIZE = 100


def normalize_name(value: str) -> str:
    return value.strip().casefold()


def require_string_list(payload: dict, field_name: str) -> list[str]:
    raw_value = payload.get(field_name, [])
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise RuntimeError(f'The "{field_name}" field must be an array.')

    values: list[str] = []
    for item in raw_value:
        if not isinstance(item, str):
            raise RuntimeError(f'The "{field_name}" field must contain only strings.')
        normalized = item.strip()
        if normalized:
            values.append(normalized)
    return values


def load_targets_file(path: pathlib.Path) -> tuple[list[str], list[str]]:
    if not path.exists():
        raise RuntimeError(f"Deployment targets file not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Deployment targets file must contain a JSON object.")

    return require_string_list(payload, "devices"), require_string_list(payload, "groups")


def get_access_token() -> str:
    token_response = request_keycloak_token()
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Keycloak token response does not contain access_token")
    return access_token


def raise_http_error_with_context(exc: urllib.error.HTTPError, stage: str, details: dict | None = None) -> None:
    context = {
        "stage": stage,
        "httpError": dump_http_error(exc),
    }
    if details:
        context["details"] = details
    raise RuntimeError(json.dumps(context))


def parse_device_collection(response: dict) -> tuple[list[dict], int | None]:
    payload = response.get("response", response)
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        if isinstance(payload.get("deviceList"), list):
            total = payload.get("total")
            return payload["deviceList"], total if isinstance(total, int) else None
        if isinstance(payload.get("devicesCustom"), list):
            total = payload.get("total")
            return payload["devicesCustom"], total if isinstance(total, int) else None
        available_keys = sorted(str(key) for key in payload.keys())
        raise RuntimeError(
            json.dumps(
                {
                    "message": "Unexpected Barbara API response for device collection",
                    "availableKeys": available_keys,
                }
            )
        )
    raise RuntimeError(
        json.dumps(
            {
                "message": "Unexpected Barbara API response for device collection",
                "payloadType": type(payload).__name__,
            }
        )
    )


def list_all_devices(access_token: str) -> list[dict]:
    devices: list[dict] = []
    from_index = 0

    while True:
        try:
            response = authorized_json_request(
                "GET",
                "/api/v1/devices",
                access_token,
                query={
                    "from": str(from_index),
                    "size": str(PAGE_SIZE),
                },
            )
        except urllib.error.HTTPError as exc:
            raise_http_error_with_context(
                exc,
                "list_all_devices",
                {"from": from_index, "size": PAGE_SIZE},
            )
        current_page, total = parse_device_collection(response)
        devices.extend(current_page)

        if not current_page:
            break
        if total is not None and len(devices) >= total:
            break
        if len(current_page) < PAGE_SIZE:
            break

        from_index += len(current_page)

    return devices


def list_all_groups(access_token: str) -> list[dict]:
    try:
        response = authorized_json_request("GET", "/api/v1/groups", access_token)
    except urllib.error.HTTPError as exc:
        raise_http_error_with_context(exc, "list_all_groups")
    payload = response.get("response", response)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("groupList"), list):
            return payload["groupList"]
        available_keys = sorted(str(key) for key in payload.keys())
        raise RuntimeError(
            json.dumps(
                {
                    "message": "Unexpected Barbara API response for group list",
                    "availableKeys": available_keys,
                }
            )
        )
    raise RuntimeError(
        json.dumps(
            {
                "message": "Unexpected Barbara API response for group list",
                "payloadType": type(payload).__name__,
            }
        )
    )


def map_by_name(items: list[dict], field_name: str) -> dict[str, dict]:
    mapped: dict[str, dict] = {}
    for item in items:
        value = item.get(field_name)
        if isinstance(value, str) and value.strip():
            mapped[normalize_name(value)] = item
    return mapped


def map_devices_by_id(devices: list[dict]) -> dict[str, dict]:
    mapped: dict[str, dict] = {}
    for device in devices:
        device_id = device.get("_id")
        if isinstance(device_id, str) and device_id:
            mapped[device_id] = device
    return mapped


def resolve_group_devices(group: dict, devices_by_id: dict[str, dict]) -> list[dict]:
    group_devices = group.get("devices", [])
    if not isinstance(group_devices, list):
        raise RuntimeError(
            json.dumps(
                {
                    "message": "Barbara group response does not contain a valid devices array",
                    "groupId": group.get("_id"),
                    "groupName": group.get("name"),
                }
            )
        )

    resolved_devices: list[dict] = []
    missing_device_ids: list[str] = []
    for device_id in group_devices:
        if not isinstance(device_id, str) or not device_id:
            continue
        device = devices_by_id.get(device_id)
        if device is None:
            missing_device_ids.append(device_id)
            continue
        resolved_devices.append(device)

    if missing_device_ids:
        raise RuntimeError(
            json.dumps(
                {
                    "message": "Some devices referenced by the Barbara group were not found in the company device list",
                    "groupId": group.get("_id"),
                    "groupName": group.get("name"),
                    "missingDeviceIds": missing_device_ids,
                }
            )
        )

    return resolved_devices


def dedupe_devices_by_name(devices: list[dict]) -> list[dict]:
    unique_devices: dict[str, dict] = {}
    for device in devices:
        device_name = device.get("deviceName")
        if isinstance(device_name, str) and device_name.strip():
            unique_devices[normalize_name(device_name)] = {
                "_id": device.get("_id"),
                "deviceName": device_name,
                "alive": device.get("alive"),
            }
    return sorted(unique_devices.values(), key=lambda device: normalize_name(str(device["deviceName"])))


def main() -> int:
    targets_path = pathlib.Path(DEFAULT_TARGETS_FILE)
    resolved_targets_path = pathlib.Path(DEFAULT_RESOLVED_TARGETS_FILE)

    try:
        requested_devices, requested_groups = load_targets_file(targets_path)
        try:
            access_token = get_access_token()
        except urllib.error.HTTPError as exc:
            raise_http_error_with_context(exc, "request_keycloak_token")

        all_devices = list_all_devices(access_token)
        devices_by_name = map_by_name(all_devices, "deviceName")
        devices_by_id = map_devices_by_id(all_devices)

        all_groups = list_all_groups(access_token)
        groups_by_name = map_by_name(all_groups, "name")

        missing_devices = [name for name in requested_devices if normalize_name(name) not in devices_by_name]
        missing_groups = [name for name in requested_groups if normalize_name(name) not in groups_by_name]
        if missing_devices or missing_groups:
            raise RuntimeError(
                json.dumps(
                    {
                        "missingDevices": missing_devices,
                        "missingGroups": missing_groups,
                    }
                )
            )

        resolved_devices = [devices_by_name[normalize_name(name)] for name in requested_devices]
        resolved_groups = []
        for group_name in requested_groups:
            group = groups_by_name[normalize_name(group_name)]
            group_id = group.get("_id")
            if not isinstance(group_id, str) or not group_id:
                raise RuntimeError(f"Barbara group response does not contain a valid _id for group: {group_name}")

            group_devices = resolve_group_devices(group, devices_by_id)
            resolved_groups.append(
                {
                    "_id": group_id,
                    "name": group.get("name"),
                    "deviceCount": len(group_devices),
                }
            )
            resolved_devices.extend(group_devices)

        unique_devices = dedupe_devices_by_name(resolved_devices)
        resolved_targets_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_targets_path.write_text(
            json.dumps(
                {
                    "requested": {
                        "devices": requested_devices,
                        "groups": requested_groups,
                    },
                    "resolved": {
                        "groups": resolved_groups,
                        "devices": unique_devices,
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        print(
            json.dumps(
                {
                    "status": "ok",
                    "targetsFile": str(targets_path),
                    "resolvedTargetsFile": str(resolved_targets_path),
                    "requestedDevices": len(requested_devices),
                    "requestedGroups": len(requested_groups),
                    "resolvedDevices": len(unique_devices),
                },
                indent=2,
            )
        )
        return 0
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
