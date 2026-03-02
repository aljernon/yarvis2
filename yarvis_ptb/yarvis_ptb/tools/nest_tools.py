import json
import logging
from datetime import datetime, timedelta, timezone

import requests

from yarvis_ptb.settings import PROJECT_ROOT
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class NestDeviceError(Exception):
    """Raised when a Nest device lookup or operation fails."""


NEST_CONFIG_PATH = PROJECT_ROOT / "nest_config.json"
NEST_TOKEN_PATH = PROJECT_ROOT / "nest_token.json"

SDM_API_BASE = "https://smartdevicemanagement.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _load_nest_config() -> dict:
    with open(NEST_CONFIG_PATH) as f:
        return json.load(f)


def _load_nest_token() -> dict:
    """Load token, refreshing if expired."""
    with open(NEST_TOKEN_PATH) as f:
        token_data = json.load(f)

    created_at = token_data.get("created_at")
    expires_in = token_data.get("expires_in", 3600)
    if created_at:
        created = datetime.fromisoformat(created_at)
        if datetime.now(timezone.utc) >= created + timedelta(seconds=expires_in - 60):
            token_data = _refresh_token(token_data)

    return token_data


def _refresh_token(token_data: dict) -> dict:
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise ValueError("No refresh token available. Re-run nest_auth.py.")

    config = _load_nest_config()
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
        },
    )
    resp.raise_for_status()
    new_token = resp.json()

    save_data = {
        "access_token": new_token["access_token"],
        "expires_in": new_token.get("expires_in", 3600),
        "refresh_token": new_token.get("refresh_token", refresh_token),
        "token_type": new_token.get("token_type", "Bearer"),
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    with open(NEST_TOKEN_PATH, "w") as f:
        json.dump(save_data, f, indent=2)
    return save_data


def _nest_api_get(path: str) -> requests.Response:
    token_data = _load_nest_token()
    headers = {"Authorization": f"Bearer {token_data['access_token']}"}
    return requests.get(f"{SDM_API_BASE}{path}", headers=headers)


def _nest_api_post(path: str, body: dict) -> requests.Response:
    token_data = _load_nest_token()
    headers = {
        "Authorization": f"Bearer {token_data['access_token']}",
        "Content-Type": "application/json",
    }
    return requests.post(f"{SDM_API_BASE}{path}", headers=headers, json=body)


def _get_project_id() -> str:
    config = _load_nest_config()
    return config["project_id"]


def _get_device_display_name(device: dict) -> str:
    """Get the best display name: custom name > room name > device id."""
    traits = device.get("traits", {})
    custom_name = traits.get("sdm.devices.traits.Info", {}).get("customName", "")
    if custom_name:
        return custom_name
    # Fall back to room name from parentRelations
    for rel in device.get("parentRelations", []):
        if rel.get("displayName"):
            return rel["displayName"]
    return device.get("name", "unknown")


def _format_device_summary(device: dict) -> dict:
    """Extract useful info from a device response."""
    traits = device.get("traits", {})
    dtype = device.get("type", "").split(".")[-1]
    display_name = _get_device_display_name(device)

    summary = {"type": dtype, "name": display_name, "device_id": device.get("name", "")}

    # Thermostat traits
    if "sdm.devices.traits.Temperature" in traits:
        temp_c = traits["sdm.devices.traits.Temperature"].get(
            "ambientTemperatureCelsius"
        )
        if temp_c is not None:
            summary["temperature_c"] = temp_c
            summary["temperature_f"] = round(temp_c * 9 / 5 + 32, 1)

    if "sdm.devices.traits.ThermostatMode" in traits:
        summary["mode"] = traits["sdm.devices.traits.ThermostatMode"].get("mode")
        summary["available_modes"] = traits["sdm.devices.traits.ThermostatMode"].get(
            "availableModes"
        )

    if "sdm.devices.traits.ThermostatTemperatureSetpoint" in traits:
        sp = traits["sdm.devices.traits.ThermostatTemperatureSetpoint"]
        if "heatCelsius" in sp:
            summary["heat_setpoint_c"] = sp["heatCelsius"]
            summary["heat_setpoint_f"] = round(sp["heatCelsius"] * 9 / 5 + 32, 1)
        if "coolCelsius" in sp:
            summary["cool_setpoint_c"] = sp["coolCelsius"]
            summary["cool_setpoint_f"] = round(sp["coolCelsius"] * 9 / 5 + 32, 1)

    if "sdm.devices.traits.ThermostatHvac" in traits:
        summary["hvac_status"] = traits["sdm.devices.traits.ThermostatHvac"].get(
            "status"
        )

    if "sdm.devices.traits.Humidity" in traits:
        summary["humidity_pct"] = traits["sdm.devices.traits.Humidity"].get(
            "ambientHumidityPercent"
        )

    if "sdm.devices.traits.Connectivity" in traits:
        summary["connectivity"] = traits["sdm.devices.traits.Connectivity"].get(
            "status"
        )

    # Camera traits
    if "sdm.devices.traits.CameraLiveStream" in traits:
        summary["has_live_stream"] = True

    return summary


class NestDeviceTool(LocalTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="nest_device",
            description=(
                "Interact with Google Nest smart home devices. Can list devices, get status "
                "(temperature, humidity, mode, HVAC status), and control thermostats "
                "(set mode, set temperature)."
            ),
            args=[
                ArgSpec(
                    name="action",
                    type=str,
                    description=(
                        'Action to perform. One of: "list", "status", "set_mode", "set_temperature".'
                    ),
                    is_required=True,
                ),
                ArgSpec(
                    name="device_name",
                    type=str,
                    description=(
                        "Custom name of the device (e.g. 'Living Room'). "
                        "Required for status, set_mode, set_temperature. "
                        "Use 'list' first to see available devices."
                    ),
                    is_required=False,
                ),
                ArgSpec(
                    name="mode",
                    type=str,
                    description='Thermostat mode for set_mode action. One of: "HEAT", "COOL", "HEATCOOL", "OFF".',
                    is_required=False,
                ),
                ArgSpec(
                    name="temperature_f",
                    type=float,
                    description="Target temperature in Fahrenheit for set_temperature action. In HEATCOOL mode, sets a +-3°F range around the target.",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        action = kwargs["action"]
        try:
            if action == "list":
                return self._list_devices()
            elif action == "status":
                return self._get_status(kwargs.get("device_name"))
            elif action == "set_mode":
                return self._set_mode(kwargs.get("device_name"), kwargs.get("mode"))
            elif action == "set_temperature":
                return self._set_temperature(
                    kwargs.get("device_name"), kwargs.get("temperature_f")
                )
            else:
                return ToolResult.error(
                    f"Invalid action: {action}. Must be one of: list, status, set_mode, set_temperature."
                )
        except NestDeviceError as e:
            return ToolResult.error(str(e))
        except FileNotFoundError:
            return ToolResult.error(
                "Nest token files not found. Run nest_auth.py to authenticate."
            )
        except Exception as e:
            return ToolResult.error(f"Nest API error: {e}")

    def _find_device(self, device_name: str | None) -> dict:
        """Find device by custom name. Raises NestDeviceError if not found."""
        if not device_name:
            raise NestDeviceError("device_name is required for this action.")
        project_id = _get_project_id()
        resp = _nest_api_get(f"/enterprises/{project_id}/devices")
        resp.raise_for_status()
        devices = resp.json().get("devices", [])
        for d in devices:
            if _get_device_display_name(d).lower() == device_name.lower():
                return d
        names = [_get_device_display_name(d) for d in devices]
        raise NestDeviceError(
            f"Device '{device_name}' not found. Available: {', '.join(names)}"
        )

    def _list_devices(self) -> ToolResult:
        project_id = _get_project_id()
        resp = _nest_api_get(f"/enterprises/{project_id}/devices")
        resp.raise_for_status()
        devices = resp.json().get("devices", [])
        if not devices:
            return ToolResult.success("No Nest devices found.")
        summaries = [_format_device_summary(d) for d in devices]
        return ToolResult.success(json.dumps(summaries, indent=2))

    def _get_status(self, device_name: str | None) -> ToolResult:
        device = self._find_device(device_name)
        return ToolResult.success(json.dumps(_format_device_summary(device), indent=2))

    def _set_mode(self, device_name: str | None, mode: str | None) -> ToolResult:
        if not mode:
            raise NestDeviceError("mode is required for set_mode action.")
        device = self._find_device(device_name)
        device_id = device["name"]
        resp = _nest_api_post(
            f"/{device_id}:executeCommand",
            {
                "command": "sdm.devices.commands.ThermostatMode.SetMode",
                "params": {"mode": mode.upper()},
            },
        )
        resp.raise_for_status()
        return ToolResult.success(f"Thermostat mode set to {mode.upper()}.")

    def _set_temperature(
        self, device_name: str | None, temperature_f: float | None
    ) -> ToolResult:
        if temperature_f is None:
            raise NestDeviceError(
                "temperature_f is required for set_temperature action."
            )
        device = self._find_device(device_name)

        # Determine command based on current mode
        traits = device.get("traits", {})
        mode = traits.get("sdm.devices.traits.ThermostatMode", {}).get("mode", "HEAT")
        device_id = device["name"]
        temp_c = (temperature_f - 32) * 5 / 9

        if mode == "HEAT":
            command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat"
            params = {"heatCelsius": round(temp_c, 1)}
        elif mode == "COOL":
            command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool"
            params = {"coolCelsius": round(temp_c, 1)}
        elif mode == "HEATCOOL":
            # For HEATCOOL, set both (use a 3°F range)
            range_c = 1.7  # ~3°F
            command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetRange"
            params = {
                "heatCelsius": round(temp_c - range_c, 1),
                "coolCelsius": round(temp_c + range_c, 1),
            }
        else:
            raise NestDeviceError(
                f"Cannot set temperature when mode is {mode}. Set mode to HEAT, COOL, or HEATCOOL first."
            )

        resp = _nest_api_post(
            f"/{device_id}:executeCommand", {"command": command, "params": params}
        )
        resp.raise_for_status()
        return ToolResult.success(
            f"Temperature set to {temperature_f}°F ({round(temp_c, 1)}°C)."
        )


def get_nest_tools() -> list[LocalTool]:
    if not NEST_TOKEN_PATH.exists():
        logger.info(
            "Nest token file not found at %s. Disabling Nest tools.", NEST_TOKEN_PATH
        )
        return []
    return [NestDeviceTool()]
