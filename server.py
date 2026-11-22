"""Garmin MCP server.

Gives Claude (or any other MCP client) read-only access to your own Garmin
Connect data: sleep, HRV, recovery, activities, training load and performance
metrics.

Sign-in uses python-garminconnect. Tokens are saved in GARMIN_TOKEN_DIR
(default ~/.garminconnect) and refreshed automatically, so the password is only
used when there is no valid saved session.
"""

import functools
import json
import logging
import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

try:  # .env is only used when running from a git checkout; the bundle passes env vars.
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv(Path(__file__).with_name(".env"))

__version__ = "0.2.0"

log = logging.getLogger("garmin-mcp")


def _env(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    # An unfilled MCPB placeholder can arrive literally as "${user_config.x}".
    if not value or value.startswith("${"):
        return None
    return value


# stdio (default) is the Claude Desktop bundle; http is the remote server (see remote_auth.py).
REMOTE = (os.getenv("MCP_TRANSPORT") or "stdio").strip().lower() == "http"
_remote_config = None
if REMOTE:
    import remote_auth  # not shipped in the .mcpb bundle, which only runs stdio

    _remote_config = remote_auth.RemoteConfig.from_env()

EMAIL = _env("GARMIN_EMAIL")
PASSWORD = _env("GARMIN_PASSWORD")
_default_token_dir = str(_remote_config.data_dir / "garminconnect") if _remote_config else "~/.garminconnect"
TOKEN_DIR = str(Path(_env("GARMIN_TOKEN_DIR") or _default_token_dir).expanduser())
if _remote_config:
    remote_auth.seed_garmin_tokens(Path(TOKEN_DIR))

MAX_RESULT_CHARS = 90_000

INSTRUCTIONS = """\
Read-only access to the user's own Garmin Connect data. Dates are YYYY-MM-DD in
the user's local time; leave a date out to mean today.

Good starting points:
- get_recovery_snapshot: "how recovered am I", "should I train hard today".
- get_weekly_training_load: training volume, intensity and sport mix per week.
- get_wellness_trend: how one metric (sleep, HRV, resting HR, readiness, ...) moved over days.
- get_activity_details: laps, heart rate zones and weather for one workout.

Sleep, HRV and daily summaries are compact by default. Pass detailed=true only
when the raw time series is really needed.

If a tool says Garmin needs a verification code, ask the user for the code Garmin
just sent them and call garmin_submit_mfa_code with it.
"""

if _remote_config:
    _oauth = remote_auth.GarminOAuthProvider(_remote_config)
    mcp = MCPServer(
        name="Garmin",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth_server_provider=_oauth,
        auth=_remote_config.auth_settings(),
    )
    remote_auth.add_routes(mcp, _oauth)
else:
    mcp = MCPServer(name="Garmin", version=__version__, instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

# --------------------------------------------------------------------------
# Sign-in
# --------------------------------------------------------------------------

MFA_MESSAGE = (
    "Garmin needs a verification code to finish signing in and has just sent one "
    "by email or SMS. Ask the user for the code, then call garmin_submit_mfa_code."
)

_lock = threading.RLock()
_api: Garmin | None = None  # stdio: the one local user
_pending_mfa: tuple[Garmin, Any] | None = None
_apis: dict[str, Garmin] = {}  # http: one signed-in client per person, keyed by OAuth subject

if _remote_config:
    # A fresh Garmin sign-in on the login page replaces any cached client for that person.
    _oauth.on_new_session = lambda subject: _apis.pop(subject, None)


def _current_subject() -> str:
    """The person the request's OAuth token was issued to (remote mode)."""
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    if token is None or not token.subject:
        raise ToolError("Not signed in. Connect the Garmin connector in Claude again.")
    return token.subject


def _remote_garmin() -> Garmin:
    subject = _current_subject()
    with _lock:
        api = _apis.get(subject)
        if api is None:
            api = Garmin()
            try:
                api.login(_remote_config.token_dir(subject, legacy_dir=TOKEN_DIR))
            except GarminConnectAuthenticationError as e:
                raise ToolError(
                    "Your Garmin session on this server is missing or has expired. In Claude, "
                    "disconnect the Garmin connector and connect it again to sign in to Garmin."
                ) from e
            _apis[subject] = api
        return api


def _forget_session() -> None:
    """Drop the cached client after Garmin rejected it, so the next call signs in again."""
    global _api
    with _lock:
        if REMOTE:
            try:
                _apis.pop(_current_subject(), None)
            except ToolError:
                pass
        else:
            _api = None


def _token_login() -> Garmin:
    """Sign in from saved tokens. Refreshed tokens are written back automatically."""
    api = Garmin()
    api.login(TOKEN_DIR)
    return api


def _credential_login() -> Garmin:
    global _pending_mfa
    if not EMAIL or not PASSWORD:
        where = (
            "Set the GARMIN_TOKENS_B64 secret on the server (see README, 'Run it remotely')."
            if REMOTE
            else "Add them in the extension settings in Claude Desktop (Settings > Extensions > Garmin Connect)."
        )
        raise ToolError(f"No saved Garmin session was found and no Garmin email/password is configured. {where}")
    api = Garmin(EMAIL, PASSWORD, return_on_mfa=True)
    try:
        status, state = api.login()
    except GarminConnectAuthenticationError as e:
        raise ToolError(
            f"Garmin sign-in failed ({e}). Check the email and password in the extension settings."
        ) from e
    if status == "needs_mfa":
        _pending_mfa = (api, state)
        raise ToolError(MFA_MESSAGE)
    api.client.dump(TOKEN_DIR)
    return _token_login()


def _garmin() -> Garmin:
    """Return a signed-in client, signing in on first use."""
    global _api
    if REMOTE:
        return _remote_garmin()
    with _lock:
        if _api is not None:
            return _api
        if _pending_mfa is not None:
            raise ToolError(MFA_MESSAGE)
        try:
            _api = _token_login()
        except GarminConnectAuthenticationError:
            # No tokens yet, or they were rejected: fall back to email/password.
            _api = _credential_login()
        return _api


@mcp.tool(
    title="Submit Garmin verification code",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
def garmin_submit_mfa_code(
    code: Annotated[str, Field(description="The verification code Garmin sent by email or SMS.")],
) -> str:
    """Finish signing in to Garmin with the verification (MFA) code the user received.
    Only call this after another Garmin tool said a code is needed."""
    global _api, _pending_mfa
    if REMOTE:
        return (
            "Not needed on this server: you sign in to Garmin, including any verification code, "
            "on the login page that opens when you connect the connector in Claude."
        )
    code = re.sub(r"\s", "", code)
    if not re.fullmatch(r"\d{4,10}", code):
        raise ToolError("The verification code should be 4-10 digits.")
    with _lock:
        if _api is not None:
            return "Already signed in to Garmin. No code is needed."
        if _pending_mfa is None:
            return "No Garmin sign-in is waiting for a code. Call a Garmin tool first."
        api, state = _pending_mfa
        try:
            api.resume_login(state, code)
        except GarminConnectAuthenticationError as e:
            # A wrong code keeps the pending sign-in so the user can try again.
            raise ToolError(f"Garmin did not accept the code ({e}). Check it and try again.") from e
        except Exception as e:
            _pending_mfa = None
            raise ToolError(
                f"Signing in with the code failed ({e}). Call a Garmin tool to start over; "
                "Garmin will send a new code."
            ) from e
        api.client.dump(TOKEN_DIR)
        _pending_mfa = None
        _api = _token_login()
    return "Signed in to Garmin. The session is saved, so a code is only needed again when it expires."


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

# Identifiers, profile details and GPS positions that add nothing to an answer.
DROP_KEYS = {
    "userProfilePK", "userProfilePk", "userProfileId", "userId", "ownerId",
    "ownerDisplayName", "ownerFullName", "ownerProfileImageUrlSmall",
    "ownerProfileImageUrlMedium", "ownerProfileImageUrlLarge", "userRoles",
    "uuid", "rule", "privacy", "deviceId", "startLatitude", "startLongitude",
    "endLatitude", "endLongitude", "latitude", "longitude",
}


def _compact(obj: Any, max_list: int | None = 30) -> Any:
    """Drop nulls, empty values and noise keys; replace long lists (time series) with a count."""
    if isinstance(obj, float):
        return round(obj, 2)
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in DROP_KEYS:
                continue
            value = _compact(value, max_list)
            if value is None or value == "" or value == [] or value == {}:
                continue
            out[key] = value
        return out
    if isinstance(obj, list):
        if max_list is not None and len(obj) > max_list:
            return f"[{len(obj)} samples omitted; ask with detailed=true]"
        return [_compact(v, max_list) for v in obj]
    return obj


def _clean(obj: Any) -> Any:
    """Recursively drop None and empty values from hand-built summaries."""
    if isinstance(obj, dict):
        out = {k: _clean(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v is not None and v != "" and v != [] and v != {}}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _r(x: Any, digits: int = 1) -> float | None:
    return round(x, digits) if _num(x) else None


def _mins(seconds: Any) -> int | None:
    return round(seconds / 60) if _num(seconds) else None


def _hours(seconds: Any) -> float | None:
    return round(seconds / 3600, 2) if _num(seconds) else None


def _km(meters: Any) -> float | None:
    return round(meters / 1000, 2) if _num(meters) and meters else None


def _hms(seconds: Any) -> str | None:
    if not _num(seconds):
        return None
    total = round(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _pace(speed_mps: Any, per_meters: int = 1000) -> str | None:
    """Speed in m/s -> pace as m:ss per km (or per 100 m for swimming)."""
    if not _num(speed_mps) or speed_mps <= 0:
        return None
    return _hms(per_meters / speed_mps)


def _local_ms(ms: Any) -> str | None:
    """Garmin's '...Local' millisecond timestamps are already shifted to local time."""
    if not _num(ms):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _avg(values: list[Any]) -> float | None:
    nums = [v for v in values if _num(v)]
    return round(sum(nums) / len(nums), 1) if nums else None


def _parse_day(value: str | None, field: str = "date_str") -> date:
    text = (value or "").strip().lower()
    if not text or text == "today":
        return date.today()
    if text == "yesterday":
        return date.today() - timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} must be a date like 2026-01-31, got {value!r}") from None


def _check_span(start: date, end: date, max_days: int) -> None:
    if end < start:
        raise ValueError("end_date is before start_date.")
    if (end - start).days + 1 > max_days:
        raise ValueError(f"The date range can be at most {max_days} days.")


def _activity_id(value: str | int) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,20}", text):
        raise ValueError("activity_id must be the numeric id from an activity list.")
    return text


def _activity_type(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip().lower()
    if not re.fullmatch(r"[a-z_]{2,40}", text):
        raise ValueError("activity_type must be a Garmin type key such as running, cycling or strength_training.")
    return text


def _parallel(calls: dict[str, Callable[[], Any]]) -> tuple[dict[str, Any], dict[str, str]]:
    """Run independent Garmin calls at once. Returns (results, errors) by name."""
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(calls))) as pool:
        futures = {name: pool.submit(fn) for name, fn in calls.items()}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except (GarminConnectAuthenticationError, GarminConnectTooManyRequestsError):
                raise
            except Exception as e:  # one missing metric should not sink the whole answer
                log.debug("%s failed: %s", name, e)
                errors[name] = str(e)[:200] or type(e).__name__
    return results, errors


def _limit_size(result: Any) -> Any:
    text = json.dumps(result, default=str, ensure_ascii=False)
    if len(text) <= MAX_RESULT_CHARS:
        return result
    return {
        "truncated": True,
        "note": f"The result was {len(text)} characters, so only the start is shown. "
        "Ask for a shorter period, fewer items or detailed=false.",
        "partial_json": text[:MAX_RESULT_CHARS],
    }


def garmin_tool(title: str):
    """Register a read-only Garmin tool with friendly errors and a size cap."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # ToolError messages reach the model; any other exception is reported
            # by the SDK as a bare "Error executing tool".
            try:
                result = fn(*args, **kwargs)
            except GarminConnectAuthenticationError as e:
                _forget_session()  # sign in again on the next call
                raise ToolError(f"Garmin rejected the session ({e}). Try the request again.") from e
            except GarminConnectTooManyRequestsError as e:
                raise ToolError("Garmin is rate limiting requests. Wait a few minutes and try again.") from e
            except GarminConnectNotFoundError as e:
                raise ToolError(f"Garmin has no data for that request ({e}).") from e
            except GarminConnectConnectionError as e:
                raise ToolError(f"Could not get data from Garmin Connect ({e}).") from e
            except (ValueError, RuntimeError) as e:
                raise ToolError(str(e)) from e
            return _limit_size(result)

        return mcp.tool(title=title, annotations=READ_ONLY)(wrapper)

    return decorator


DateArg = Annotated[str | None, Field(description="Date as YYYY-MM-DD. Defaults to today.")]
DetailedArg = Annotated[bool, Field(description="Include raw time series (large). Usually not needed.")]

# --------------------------------------------------------------------------
# Summaries of raw Garmin responses
# --------------------------------------------------------------------------


def _sleep_summary(raw: dict) -> dict:
    dto = raw.get("dailySleepDTO") or {}
    scores = dto.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    summary = {
        "date": dto.get("calendarDate"),
        "sleep_start": _local_ms(dto.get("sleepStartTimestampLocal")),
        "sleep_end": _local_ms(dto.get("sleepEndTimestampLocal")),
        "sleep_hours": _hours(dto.get("sleepTimeSeconds")),
        "deep_min": _mins(dto.get("deepSleepSeconds")),
        "light_min": _mins(dto.get("lightSleepSeconds")),
        "rem_min": _mins(dto.get("remSleepSeconds")),
        "awake_min": _mins(dto.get("awakeSleepSeconds")),
        "awake_count": dto.get("awakeCount"),
        "nap_min": _mins(dto.get("napTimeSeconds")) or None,
        "sleep_score": overall.get("value"),
        "sleep_quality": overall.get("qualifierKey"),
        "score_factors": {
            key: _clean({"value": v.get("value"), "rating": v.get("qualifierKey")})
            for key, v in scores.items()
            if key != "overall" and isinstance(v, dict)
        },
        "feedback": dto.get("sleepScoreFeedback"),
        "insight": dto.get("sleepScorePersonalizedInsight") or dto.get("sleepScoreInsight"),
        "avg_sleep_hr": dto.get("avgHeartRate"),
        "resting_hr": raw.get("restingHeartRate"),
        "avg_overnight_hrv": raw.get("avgOvernightHrv"),
        "hrv_status": raw.get("hrvStatus"),
        "body_battery_change": raw.get("bodyBatteryChange"),
        "avg_respiration": dto.get("averageRespirationValue"),
        "avg_spo2": dto.get("averageSpO2Value"),
        "lowest_spo2": dto.get("lowestSpO2Value"),
        "avg_sleep_stress": dto.get("avgSleepStress"),
        "restless_moments": raw.get("restlessMomentsCount"),
    }
    if not dto.get("sleepTimeSeconds"):
        summary["note"] = "No sleep recorded for this date."
    return _clean(summary)


def _sleep_night(entry: dict) -> dict:
    """One night from the sleep range endpoint."""
    v = entry.get("values") or {}
    return _clean({
        "date": entry.get("calendarDate"),
        "sleep_score": v.get("sleepScore"),
        "quality": v.get("sleepScoreQuality"),
        "sleep_hours": _hours(v.get("totalSleepTimeInSeconds")),
        "sleep_need_hours": _r(v["sleepNeed"] / 60, 2) if _num(v.get("sleepNeed")) else None,
        "deep_min": _mins(v.get("deepTime")),
        "light_min": _mins(v.get("lightTime")),
        "rem_min": _mins(v.get("remTime")),
        "awake_min": _mins(v.get("awakeTime")),
        "sleep_start": _local_ms(v.get("localSleepStartTimeInMillis")),
        "sleep_end": _local_ms(v.get("localSleepEndTimeInMillis")),
        "resting_hr": v.get("restingHeartRate"),
        "avg_overnight_hrv": v.get("avgOvernightHrv"),
        "hrv_status": v.get("hrvStatus"),
        "body_battery_change": v.get("bodyBatteryChange"),
        "respiration": _r(v.get("respiration")),
        "spo2": _r(v.get("spO2")),
    })


def _hrv_summary(s: dict) -> dict:
    baseline = s.get("baseline") or {}
    return _clean({
        "date": s.get("calendarDate"),
        "last_night_avg": s.get("lastNightAvg"),
        "last_night_5min_high": s.get("lastNight5MinHigh"),
        "weekly_avg": s.get("weeklyAvg"),
        "status": s.get("status"),
        "baseline_balanced_range": (
            [baseline.get("balancedLow"), baseline.get("balancedUpper")]
            if baseline.get("balancedLow") is not None else None
        ),
        "baseline_low_upper": baseline.get("lowUpper"),
        "feedback": s.get("feedbackPhrase"),
    })


def _readiness(e: dict) -> dict:
    def factor(prefix: str, **extra: Any) -> dict:
        return _clean({
            **extra,
            "percent": e.get(f"{prefix}FactorPercent"),
            "feedback": e.get(f"{prefix}FactorFeedback"),
        })

    return _clean({
        "date": e.get("calendarDate"),
        "time": e.get("timestampLocal"),
        "context": e.get("inputContext"),
        "score": e.get("score"),
        "level": e.get("level"),
        "feedback": e.get("feedbackShort"),
        "feedback_detail": e.get("feedbackLong"),
        "recovery_time_hours": _r(e["recoveryTime"] / 60) if _num(e.get("recoveryTime")) else None,
        "acute_load": e.get("acuteLoad"),
        "factors": {
            "sleep": factor("sleepScore", sleep_score=e.get("sleepScore")),
            "recovery_time": factor("recoveryTime"),
            "acute_chronic_load": factor("acwr"),
            "hrv": factor("hrv", hrv_weekly_avg=e.get("hrvWeeklyAverage")),
            "stress_history": factor("stressHistory"),
            "sleep_history": factor("sleepHistory"),
        },
    })


def _daily_summary(s: dict) -> dict:
    return _clean({
        "date": s.get("calendarDate"),
        "steps": s.get("totalSteps"),
        "step_goal": s.get("dailyStepGoal"),
        "distance_km": _km(s.get("totalDistanceMeters")),
        "floors_climbed": _r(s.get("floorsAscended"), 0),
        "calories": {"total": s.get("totalKilocalories"), "active": s.get("activeKilocalories")},
        "intensity_minutes": {
            "moderate": s.get("moderateIntensityMinutes"),
            "vigorous": s.get("vigorousIntensityMinutes"),
            "weekly_goal": s.get("intensityMinutesGoal"),
        },
        "heart_rate": {
            "resting": s.get("restingHeartRate"),
            "resting_7d_avg": s.get("lastSevenDaysAvgRestingHeartRate"),
            "min": s.get("minHeartRate"),
            "max": s.get("maxHeartRate"),
        },
        "stress": {
            "avg": s.get("averageStressLevel"),
            "max": s.get("maxStressLevel"),
            "qualifier": s.get("stressQualifier"),
            "rest_min": _mins(s.get("restStressDuration")),
            "low_min": _mins(s.get("lowStressDuration")),
            "medium_min": _mins(s.get("mediumStressDuration")),
            "high_min": _mins(s.get("highStressDuration")),
        },
        "body_battery": {
            "at_wake": s.get("bodyBatteryAtWakeTime"),
            "highest": s.get("bodyBatteryHighestValue"),
            "lowest": s.get("bodyBatteryLowestValue"),
            "latest": s.get("bodyBatteryMostRecentValue"),
            "charged": s.get("bodyBatteryChargedValue"),
            "drained": s.get("bodyBatteryDrainedValue"),
        },
        "time": {
            "highly_active_min": _mins(s.get("highlyActiveSeconds")),
            "active_min": _mins(s.get("activeSeconds")),
            "sedentary_min": _mins(s.get("sedentarySeconds")),
            "sleeping_min": _mins(s.get("sleepingSeconds")),
        },
        "spo2": {"avg": s.get("averageSpo2"), "lowest": s.get("lowestSpo2")},
        "respiration_avg_waking": s.get("avgWakingRespirationValue"),
    })


def _type_key(a: dict) -> str | None:
    return (a.get("activityType") or a.get("activityTypeDTO") or {}).get("typeKey")


def _speed_fields(type_key: str | None, speed: Any, prefix: str = "") -> dict:
    t = type_key or ""
    if any(s in t for s in ("running", "walking", "hiking")):
        return {f"{prefix}pace_per_km": _pace(speed)}
    if "swim" in t:
        return {f"{prefix}pace_per_100m": _pace(speed, 100)}
    if any(s in t for s in ("cycling", "biking", "ride")):
        return {f"{prefix}speed_kmh": _r(speed * 3.6) if _num(speed) else None}
    return {}


def _activity_summary(a: dict) -> dict:
    t = _type_key(a)
    zones = [a.get(f"hrTimeInZone_{i}") for i in range(1, 6)]
    return _clean({
        "id": a.get("activityId"),
        "name": a.get("activityName"),
        "type": t,
        "start": a.get("startTimeLocal"),
        "location": a.get("locationName"),
        "duration": _hms(a.get("duration")),
        "duration_min": _r(a["duration"] / 60) if _num(a.get("duration")) else None,
        "distance_km": _km(a.get("distance")),
        **_speed_fields(t, a.get("averageSpeed"), "avg_"),
        "elevation_gain_m": _r(a.get("elevationGain"), 0),
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "hr_zone_minutes": [_r((z or 0) / 60) for z in zones] if any(_num(z) for z in zones) else None,
        "calories": a.get("calories"),
        "training_load": _r(a.get("activityTrainingLoad")),
        "aerobic_te": _r(a.get("aerobicTrainingEffect")),
        "anaerobic_te": _r(a.get("anaerobicTrainingEffect")),
        "te_label": a.get("trainingEffectLabel"),
        "vo2max": a.get("vO2MaxValue"),
        "avg_cadence": _r(a.get("averageRunningCadenceInStepsPerMinute") or a.get("averageBikingCadenceInRevPerMinute"), 0),
        "avg_power": a.get("avgPower"),
        "normalized_power": a.get("normPower"),
        "intensity_minutes": {
            "moderate": a.get("moderateIntensityMinutes"),
            "vigorous": a.get("vigorousIntensityMinutes"),
        },
        "is_pr": a.get("isPR") or None,
        "description": a.get("description"),
    })


def _lap(lap: dict, type_key: str | None) -> dict:
    return _clean({
        "lap": lap.get("lapIndex"),
        "intensity": lap.get("intensityType"),
        "distance_km": _km(lap.get("distance")),
        "duration": _hms(lap.get("duration")),
        **_speed_fields(type_key, lap.get("averageSpeed")),
        **_speed_fields(type_key, lap.get("avgGradeAdjustedSpeed"), "grade_adjusted_"),
        "avg_hr": lap.get("averageHR"),
        "max_hr": lap.get("maxHR"),
        "elevation_gain_m": _r(lap.get("elevationGain"), 0),
        "elevation_loss_m": _r(lap.get("elevationLoss"), 0),
        "avg_cadence": _r(lap.get("averageRunCadence") or lap.get("averageBikeCadence"), 0),
        "avg_power": _r(lap.get("averagePower"), 0),
        "ground_contact_ms": _r(lap.get("groundContactTime"), 0),
        "stride_length_cm": _r(lap.get("strideLength"), 0),
        "avg_respiration": _r(lap.get("avgRespirationRate")),
    })


# --------------------------------------------------------------------------
# Recovery and sleep
# --------------------------------------------------------------------------


@garmin_tool("Recovery snapshot")
def get_recovery_snapshot(date_str: DateArg = None) -> dict[str, Any]:
    """One-call overview of how recovered the user is on a date: last night's sleep,
    overnight HRV versus baseline, training readiness, resting heart rate, body
    battery and stress. Use it for "should I train hard today?" type questions."""
    day = _parse_day(date_str).isoformat()
    api = _garmin()
    data, errors = _parallel({
        "sleep": lambda: api.get_sleep_data(day),
        "hrv": lambda: api.get_hrv_data(day),
        "readiness": lambda: api.get_training_readiness(day),
        "summary": lambda: api.get_user_summary(day),
    })
    result: dict[str, Any] = {"date": day}
    if data.get("sleep"):
        sleep = _sleep_summary(data["sleep"])
        sleep.pop("score_factors", None)
        result["sleep"] = sleep
    if data.get("hrv") and data["hrv"].get("hrvSummary"):
        result["hrv"] = _hrv_summary(data["hrv"]["hrvSummary"])
    entries = data.get("readiness") or []
    if entries:
        morning = next((e for e in entries if e.get("inputContext") == "AFTER_WAKEUP_RESET"), None)
        latest = max(entries, key=lambda e: e.get("timestamp") or "")
        result["training_readiness"] = _readiness(latest)
        if morning and morning is not latest:
            result["training_readiness_morning"] = _readiness(morning)
    if data.get("summary"):
        s = _daily_summary(data["summary"])
        result["resting_hr"] = s.get("heart_rate")
        result["body_battery"] = s.get("body_battery")
        result["stress"] = s.get("stress")
    if errors:
        result["unavailable"] = errors
    return _clean(result)


@garmin_tool("Sleep for one night")
def get_sleep_data(date_str: DateArg = None, detailed: DetailedArg = False) -> dict[str, Any]:
    """Sleep for the night ending on a date: duration, sleep stages, sleep score and
    its factors, overnight HRV, resting HR, SpO2, respiration and body battery change."""
    day = _parse_day(date_str).isoformat()
    raw = _garmin().get_sleep_data(day)
    if detailed:
        return _compact(raw, max_list=None)
    return _sleep_summary(raw)


@garmin_tool("Sleep over recent nights")
def get_sleep_last_n_days(
    days: Annotated[int, Field(ge=1, le=90, description="Number of nights, ending today.")] = 7,
) -> dict[str, Any]:
    """Sleep for the last N nights (score, duration, stages, HRV, resting HR), plus
    averages for the period. Good for spotting trends."""
    end = date.today()
    start = end - timedelta(days=days - 1)
    nights = [_sleep_night(e) for e in _garmin().get_sleep_daily(start.isoformat(), end.isoformat())]
    nights = [n for n in nights if n.get("sleep_hours")]
    return _clean({
        "start": start.isoformat(),
        "end": end.isoformat(),
        "nights_recorded": len(nights),
        "averages": {
            "sleep_score": _avg([n.get("sleep_score") for n in nights]),
            "sleep_hours": _avg([n.get("sleep_hours") for n in nights]),
            "deep_min": _avg([n.get("deep_min") for n in nights]),
            "rem_min": _avg([n.get("rem_min") for n in nights]),
            "avg_overnight_hrv": _avg([n.get("avg_overnight_hrv") for n in nights]),
            "resting_hr": _avg([n.get("resting_hr") for n in nights]),
        },
        "nights": nights,
    })


@garmin_tool("Heart rate variability")
def get_hrv(date_str: DateArg = None, detailed: DetailedArg = False) -> dict[str, Any]:
    """Overnight heart rate variability (HRV) for a date: last-night average, weekly
    average, status and the user's personal baseline range."""
    day = _parse_day(date_str).isoformat()
    raw = _garmin().get_hrv_data(day) or {}
    if detailed:
        return _compact(raw, max_list=None)
    if not raw.get("hrvSummary"):
        return {"date": day, "note": "No HRV recorded for this date."}
    return _hrv_summary(raw["hrvSummary"])


@garmin_tool("Daily summary")
def get_daily_summary(date_str: DateArg = None, detailed: DetailedArg = False) -> dict[str, Any]:
    """Daily summary for a date: steps, distance, calories, intensity minutes, resting/
    min/max heart rate, stress, body battery and SpO2."""
    day = _parse_day(date_str).isoformat()
    raw = _garmin().get_user_summary(day)
    if detailed:
        return _compact(raw)
    return _daily_summary(raw)


@garmin_tool("Training readiness")
def get_training_readiness(date_str: DateArg = None) -> dict[str, Any]:
    """Garmin training readiness for a date: score, level and the factors behind it
    (sleep, recovery time, acute/chronic load, HRV, stress and sleep history). A day
    can have several entries, for example on waking and after a workout."""
    day = _parse_day(date_str).isoformat()
    entries = _garmin().get_training_readiness(day) or []
    if not entries:
        return {"date": day, "note": "No training readiness recorded for this date."}
    entries = sorted(entries, key=lambda e: e.get("timestamp") or "")
    return {"date": day, "entries": [_readiness(e) for e in entries]}


@garmin_tool("Training status")
def get_training_status(date_str: DateArg = None) -> dict[str, Any]:
    """Garmin training status for a date: status (productive, maintaining, recovery,
    unproductive ...), acute vs chronic training load, 4-week load balance
    (aerobic low / aerobic high / anaerobic versus targets) and latest VO2 max."""
    day = _parse_day(date_str).isoformat()
    raw = _garmin().get_training_status(day) or {}

    def primary(mapping: dict | None) -> dict:
        devices = list((mapping or {}).values())
        return next((d for d in devices if d.get("primaryTrainingDevice")), devices[0] if devices else {})

    status = primary((raw.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData"))
    balance = primary((raw.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap"))
    vo2 = raw.get("mostRecentVO2Max") or {}
    drop = {"timestamp", "primaryTrainingDevice"}
    return _clean({
        "date": day,
        "training_status": _compact({k: v for k, v in status.items() if k not in drop}),
        "load_balance_4_weeks": _compact({k: v for k, v in balance.items() if k not in drop}),
        "vo2max": _compact({"running": vo2.get("generic"), "cycling": vo2.get("cycling")}),
    })


# --------------------------------------------------------------------------
# Activities and training load
# --------------------------------------------------------------------------

ActivityTypeArg = Annotated[
    str | None,
    Field(description="Optional Garmin activity type key, e.g. running, cycling, strength_training, swimming."),
]


@garmin_tool("Recent activities")
def get_recent_activities(
    limit: Annotated[int, Field(ge=1, le=100, description="How many activities to return.")] = 10,
    activity_type: ActivityTypeArg = None,
) -> dict[str, Any]:
    """The most recent activities (runs, rides, strength sessions etc.), newest first,
    with duration, distance, pace/speed, heart rate and zones, training load and
    training effect. Use the id with get_activity_details for laps and more."""
    activities = _garmin().get_activities(0, limit, _activity_type(activity_type)) or []
    return {"count": len(activities), "activities": [_activity_summary(a) for a in activities]}


@garmin_tool("Activities in a date range")
def get_activities_by_date(
    start_date: Annotated[str, Field(description="First date, YYYY-MM-DD.")],
    end_date: Annotated[str | None, Field(description="Last date, YYYY-MM-DD. Defaults to today.")] = None,
    activity_type: ActivityTypeArg = None,
) -> dict[str, Any]:
    """All activities between two dates (at most one year), oldest first."""
    start = _parse_day(start_date, "start_date")
    end = _parse_day(end_date, "end_date")
    _check_span(start, end, 366)
    activities = _garmin().get_activities_by_date(
        start.isoformat(), end.isoformat(), _activity_type(activity_type)
    ) or []
    activities = sorted(activities, key=lambda a: a.get("startTimeLocal") or "")
    shown = activities[:200]
    result: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(activities),
        "activities": [_activity_summary(a) for a in shown],
    }
    if len(activities) > len(shown):
        result["note"] = f"Showing the first {len(shown)} of {len(activities)}. Use a shorter range for the rest."
    return result


@garmin_tool("Activity details")
def get_activity_details(
    activity_id: Annotated[str, Field(description="Numeric activity id from an activity list.")],
) -> dict[str, Any]:
    """Details for one activity: full summary, laps/splits with pace and heart rate,
    time in each heart rate zone, and the weather during the activity."""
    aid = _activity_id(activity_id)
    api = _garmin()
    data, errors = _parallel({
        "activity": lambda: api.get_activity(aid),
        "splits": lambda: api.get_activity_splits(aid),
        "zones": lambda: api.get_activity_hr_in_timezones(aid),
        "weather": lambda: api.get_activity_weather(aid),
    })
    activity = data.get("activity")
    if not activity:
        raise RuntimeError(f"Could not load activity {aid}: {errors.get('activity', 'not found')}")
    s = activity.get("summaryDTO") or {}
    t = _type_key(activity)
    result: dict[str, Any] = {
        "id": activity.get("activityId"),
        "name": activity.get("activityName"),
        "type": t,
        "description": activity.get("description"),
        "location": activity.get("locationName"),
        "start": s.get("startTimeLocal"),
        "duration": _hms(s.get("duration")),
        "moving_duration": _hms(s.get("movingDuration")),
        "distance_km": _km(s.get("distance")),
        **_speed_fields(t, s.get("averageSpeed"), "avg_"),
        **_speed_fields(t, s.get("avgGradeAdjustedSpeed"), "grade_adjusted_"),
        "elevation_gain_m": _r(s.get("elevationGain"), 0),
        "elevation_loss_m": _r(s.get("elevationLoss"), 0),
        "avg_hr": s.get("averageHR"),
        "max_hr": s.get("maxHR"),
        "calories": s.get("calories"),
        "training_load": _r(s.get("activityTrainingLoad")),
        "aerobic_te": _r(s.get("trainingEffect")),
        "anaerobic_te": _r(s.get("anaerobicTrainingEffect")),
        "te_label": s.get("trainingEffectLabel"),
        "te_message": s.get("aerobicTrainingEffectMessage"),
        "body_battery_change": s.get("differenceBodyBattery"),
        "intensity_minutes": {
            "moderate": s.get("moderateIntensityMinutes"),
            "vigorous": s.get("vigorousIntensityMinutes"),
        },
        "running_dynamics": {
            "avg_cadence": _r(s.get("averageRunCadence"), 0),
            "stride_length_cm": _r(s.get("strideLength"), 0),
            "ground_contact_ms": _r(s.get("groundContactTime"), 0),
            "vertical_oscillation_cm": _r(s.get("verticalOscillation")),
            "vertical_ratio_pct": _r(s.get("verticalRatio")),
        },
        "avg_power": _r(s.get("averagePower"), 0),
        "normalized_power": _r(s.get("normalizedPower"), 0),
        "avg_respiration": _r(s.get("avgRespirationRate")),
    }
    laps = (data.get("splits") or {}).get("lapDTOs") or []
    if laps:
        result["laps"] = [_lap(lap, t) for lap in laps[:60]]
        if len(laps) > 60:
            result["laps_note"] = f"Showing 60 of {len(laps)} laps."
    zones = data.get("zones") or []
    if zones:
        result["hr_zones"] = [
            _clean({"zone": z.get("zoneNumber"), "from_bpm": z.get("zoneLowBoundary"), "minutes": _r((z.get("secsInZone") or 0) / 60)})
            for z in zones
        ]
    w = data.get("weather")
    if w:
        result["weather"] = _clean({
            "description": (w.get("weatherTypeDTO") or {}).get("desc"),
            "temp_c": _r((w["temp"] - 32) * 5 / 9) if _num(w.get("temp")) else None,
            "feels_like_c": _r((w["apparentTemp"] - 32) * 5 / 9) if _num(w.get("apparentTemp")) else None,
            "humidity_pct": w.get("relativeHumidity"),
            "wind_mph": w.get("windSpeed"),
            "wind_direction": w.get("windDirectionCompassPoint"),
            "station": (w.get("weatherStationDTO") or {}).get("name"),
        })
    errors.pop("weather", None)  # weather is often missing for indoor workouts
    if errors:
        result["unavailable"] = errors
    return _clean(result)


@garmin_tool("Weekly training load")
def get_weekly_training_load(
    weeks: Annotated[int, Field(ge=1, le=26, description="Number of weeks, ending with the current week.")] = 4,
) -> dict[str, Any]:
    """Training per ISO week (Monday-Sunday): sessions, hours, distance, Garmin training
    load, intensity minutes, time in heart rate zones, training-effect labels and a
    breakdown by sport. The current week is partial."""
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    first_monday = this_monday - timedelta(weeks=weeks - 1)
    activities = _garmin().get_activities_by_date(first_monday.isoformat(), today.isoformat()) or []

    weekly: dict[date, dict[str, Any]] = {
        first_monday + timedelta(weeks=i): {
            "sessions": 0, "seconds": 0.0, "meters": 0.0, "load": 0.0, "moderate": 0, "vigorous": 0,
            "zones": [0.0] * 5, "labels": Counter(), "sports": {},
        }
        for i in range(weeks)
    }
    for a in activities:
        try:
            day = datetime.fromisoformat(a["startTimeLocal"]).date()
        except (KeyError, TypeError, ValueError):
            continue
        week = weekly.get(day - timedelta(days=day.weekday()))
        if week is None:
            continue
        seconds = a.get("duration") or 0
        meters = a.get("distance") or 0
        week["sessions"] += 1
        week["seconds"] += seconds
        week["meters"] += meters
        week["load"] += a.get("activityTrainingLoad") or 0
        week["moderate"] += a.get("moderateIntensityMinutes") or 0
        week["vigorous"] += a.get("vigorousIntensityMinutes") or 0
        for i in range(5):
            week["zones"][i] += (a.get(f"hrTimeInZone_{i + 1}") or 0) / 60
        if a.get("trainingEffectLabel") not in (None, "UNKNOWN"):
            week["labels"][a["trainingEffectLabel"]] += 1
        sport = week["sports"].setdefault(_type_key(a) or "other", {"sessions": 0, "seconds": 0.0, "meters": 0.0})
        sport["sessions"] += 1
        sport["seconds"] += seconds
        sport["meters"] += meters

    out = []
    for monday, w in weekly.items():
        out.append(_clean({
            "week_start": monday.isoformat(),
            "iso_week": monday.isocalendar().week,
            "partial": monday == this_monday or None,
            "sessions": w["sessions"],
            "hours": _r(w["seconds"] / 3600, 2),
            "distance_km": _km(w["meters"]),
            "training_load": _r(w["load"], 0),
            "intensity_minutes": w["moderate"] + 2 * w["vigorous"],
            "hr_zone_minutes": [_r(z, 0) for z in w["zones"]] if any(w["zones"]) else None,
            "training_effect_labels": dict(w["labels"]),
            "by_sport": {
                k: _clean({"sessions": v["sessions"], "hours": _r(v["seconds"] / 3600, 2), "distance_km": _km(v["meters"])})
                for k, v in sorted(w["sports"].items(), key=lambda kv: -kv[1]["seconds"])
            },
        }))
    return {
        "weeks": out,
        "notes": "intensity_minutes = moderate + 2 x vigorous (Garmin's weekly goal counts it this way). "
        "hr_zone_minutes are zones 1-5.",
    }


@garmin_tool("Planned workouts")
def get_planned_workouts(
    start_date: DateArg = None,
    days: Annotated[int, Field(ge=1, le=60, description="How many days ahead to look.")] = 14,
) -> dict[str, Any]:
    """Workouts scheduled in the Garmin Connect calendar (including Garmin Coach and
    adaptive training plans) from a date and N days ahead, plus active training plans."""
    start = _parse_day(start_date, "start_date")
    end = start + timedelta(days=days - 1)
    api = _garmin()
    data, errors = _parallel({
        "scheduled": lambda: api.get_scheduled_workouts_by_period(start.isoformat(), end.isoformat()),
        "plans": lambda: api.get_active_training_plans(start.isoformat()),
    })
    result: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "scheduled_workouts": _compact(data.get("scheduled") or [], max_list=None),
        "active_training_plans": _compact((data.get("plans") or {}).get("trainingPlanList") or [], max_list=None),
    }
    if errors:
        result["unavailable"] = errors
    return result


# --------------------------------------------------------------------------
# Performance and records
# --------------------------------------------------------------------------

ENDURANCE_LEVELS = [
    ("classificationLowerLimitElite", "elite"),
    ("classificationLowerLimitSuperior", "superior"),
    ("classificationLowerLimitExpert", "expert"),
    ("classificationLowerLimitWellTrained", "well_trained"),
    ("classificationLowerLimitTrained", "trained"),
    ("classificationLowerLimitIntermediate", "intermediate"),
]


@garmin_tool("Performance metrics")
def get_performance_metrics(date_str: DateArg = None) -> dict[str, Any]:
    """Fitness and performance numbers: VO2 max (running/cycling), race time
    predictions (5K, 10K, half, marathon), endurance score, hill score, fitness age
    and lactate threshold."""
    day = _parse_day(date_str).isoformat()
    api = _garmin()
    data, errors = _parallel({
        "max_metrics": lambda: api.get_max_metrics_latest(day),
        "race": lambda: api.get_race_predictions(),
        "endurance": lambda: api.get_endurance_score(day),
        "hill": lambda: api.get_hill_score(day),
        "fitness_age": lambda: api.get_fitnessage_data(day),
        "lactate": lambda: api.get_lactate_threshold(latest=True),
    })
    result: dict[str, Any] = {"date": day}

    mm = data.get("max_metrics")
    mm = mm[0] if isinstance(mm, list) and mm else mm
    if isinstance(mm, dict):
        result["vo2max"] = _clean({
            "running": _r((mm.get("generic") or {}).get("vo2MaxPreciseValue")),
            "running_date": (mm.get("generic") or {}).get("calendarDate"),
            "cycling": _r((mm.get("cycling") or {}).get("vo2MaxPreciseValue")),
            "cycling_date": (mm.get("cycling") or {}).get("calendarDate"),
        })
    race = data.get("race") or {}
    if race:
        result["race_predictions"] = _clean({
            "5k": _hms(race.get("time5K")),
            "10k": _hms(race.get("time10K")),
            "half_marathon": _hms(race.get("timeHalfMarathon")),
            "marathon": _hms(race.get("timeMarathon")),
            "date": race.get("calendarDate"),
        })
    es = data.get("endurance") or {}
    if es.get("overallScore") is not None:
        score = es["overallScore"]
        level = next((name for key, name in ENDURANCE_LEVELS if _num(es.get(key)) and score >= es[key]), "recreational")
        result["endurance_score"] = {"score": score, "level": level, "date": es.get("calendarDate")}
    hs = data.get("hill") or {}
    if hs.get("overallScore") is not None:
        result["hill_score"] = _clean({
            "score": hs.get("overallScore"),
            "strength": hs.get("strengthScore"),
            "endurance": hs.get("enduranceScore"),
            "date": hs.get("calendarDate"),
        })
    fa = data.get("fitness_age") or {}
    if fa.get("fitnessAge") is not None:
        result["fitness_age"] = _clean({
            "fitness_age": _r(fa.get("fitnessAge")),
            "chronological_age": fa.get("chronologicalAge"),
            "achievable": _r(fa.get("achievableFitnessAge")),
            "components": _compact(fa.get("components")),
        })
    lt = data.get("lactate") or {}
    shr = lt.get("speed_and_heart_rate") or {}
    if shr.get("heartRate"):
        result["lactate_threshold"] = _clean({
            "heart_rate": shr.get("heartRate"),
            "measured": str(shr.get("calendarDate") or "")[:10],
            "running_ftp_watts": (lt.get("power") or {}).get("functionalThresholdPower"),
        })
    if errors:
        result["unavailable"] = errors
    return _clean(result)


def _pr_value(key: str, value: Any) -> dict:
    if not _num(value):
        return {}
    if "steps" in key or "pushes" in key:
        unit = "days" if "streak" in key else "steps"
        return {"value": round(value), "unit": unit}
    if "farthest" in key or "longest" in key:
        return {"distance_km": _km(value)}
    if "max.elev" in key:
        return {"elevation_m": round(value)}
    if "max.power" in key:
        return {"watts": round(value)}
    if "activeseconds" in key:
        return {"active_time": _hms(value)}
    if "max.rep.weight" in key:
        return {"value": _r(value), "unit": "as stored by Garmin (usually grams)"}
    return {"time": _hms(value)}


@garmin_tool("Personal records")
def get_personal_records() -> dict[str, Any]:
    """The user's personal records: fastest 1K/mile/5K/10K/half/marathon, longest
    run and ride, best step days and strength PRs, with the activity each was set in."""
    api = _garmin()
    data, errors = _parallel({
        "records": api.get_personal_record,
        "types": api.get_personal_record_types,
    })
    names = {t.get("id"): t.get("key", "") for t in data.get("types") or []}
    records = []
    for pr in data.get("records") or []:
        key = names.get(pr.get("typeId"), f"type_{pr.get('typeId')}")
        records.append(_clean({
            "record": key.removeprefix("pr.label.").replace(".", " "),
            **_pr_value(key, pr.get("value")),
            "date": (pr.get("activityStartDateTimeLocalFormatted") or pr.get("prStartTimeGmtFormatted") or "")[:10],
            "activity": pr.get("activityName"),
            "activity_id": pr.get("activityId") or None,
            "activity_type": pr.get("activityType"),
        }))
    result: dict[str, Any] = {"records": records}
    if errors:
        result["unavailable"] = errors
    return result


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------

TrendMetric = Literal[
    "sleep", "hrv", "resting_hr", "heart_rate", "training_readiness", "body_battery",
    "stress", "steps", "intensity_minutes", "weight", "vo2max",
]

# The value each metric's min/max/average is computed from.
TREND_MAIN_VALUE = {
    "sleep": "sleep_score", "hrv": "last_night_avg", "resting_hr": "resting_hr",
    "heart_rate": "resting_hr", "training_readiness": "score", "body_battery": "highest",
    "stress": "avg_stress", "steps": "steps", "intensity_minutes": "total",
    "weight": "weight_kg", "vo2max": "vo2max",
}


def _per_day_values(entries: list[dict]) -> list[dict]:
    return [{"date": e.get("calendarDate"), **(e.get("values") or {})} for e in entries or []]


def _trend_rows(api: Garmin, metric: str, start: str, end: str) -> list[dict]:
    if metric == "sleep":
        rows = [_sleep_night(e) for e in api.get_sleep_daily(start, end) or []]
        return [r for r in rows if r.get("sleep_hours")]
    if metric == "hrv":
        return [_hrv_summary(s) for s in (api.get_hrv_data_range(start, end) or {}).get("hrvSummaries") or []]
    if metric == "resting_hr":
        return [{"date": e.get("calendarDate"), "resting_hr": e.get("value")} for e in api.get_rhr_daily(start, end) or []]
    if metric == "heart_rate":
        return [
            _clean({"date": r["date"], "resting_hr": r.get("restingHR"), "min_avg_hr": r.get("wellnessMinAvgHR"), "max_avg_hr": r.get("wellnessMaxAvgHR")})
            for r in _per_day_values(api.get_daily_heart_rate_stats(start, end))
        ]
    if metric == "training_readiness":
        by_day: dict[str, dict] = {}
        for e in sorted(api.get_training_readiness_range(start, end) or [], key=lambda e: e.get("timestamp") or ""):
            d = e.get("calendarDate")
            # Keep the morning value: it is the one that reflects the night's recovery.
            if d not in by_day or e.get("inputContext") == "AFTER_WAKEUP_RESET":
                by_day[d] = e
        return [
            _clean({"date": d, "score": e.get("score"), "level": e.get("level"), "feedback": e.get("feedbackShort"),
                    "acute_load": e.get("acuteLoad"), "sleep_score": e.get("sleepScore")})
            for d, e in sorted(by_day.items())
        ]
    if metric == "body_battery":
        return [
            _clean({"date": r["date"], "highest": r.get("highBodyBattery"), "lowest": r.get("lowBodyBattery")})
            for r in _per_day_values(api.get_daily_body_battery_stats(start, end))
        ]
    if metric == "stress":
        return [
            _clean({"date": r["date"], "avg_stress": r.get("overallStressLevel"),
                    "rest_min": _mins(r.get("restStressDuration")), "low_min": _mins(r.get("lowStressDuration")),
                    "medium_min": _mins(r.get("mediumStressDuration")), "high_min": _mins(r.get("highStressDuration"))})
            for r in _per_day_values(api.get_daily_stress_stats(start, end))
        ]
    if metric == "steps":
        return [
            _clean({"date": e.get("calendarDate"), "steps": e.get("totalSteps"), "goal": e.get("stepGoal"), "distance_km": _km(e.get("totalDistance"))})
            for e in api.get_daily_steps(start, end) or []
        ]
    if metric == "intensity_minutes":
        return [
            {"date": e.get("calendarDate"), "moderate": e.get("moderateValue") or 0, "vigorous": e.get("vigorousValue") or 0,
             "total": (e.get("moderateValue") or 0) + 2 * (e.get("vigorousValue") or 0)}
            for e in api.get_daily_intensity_minutes_stats(start, end) or []
        ]
    if metric == "weight":
        rows = (api.get_body_composition(start, end) or {}).get("dateWeightList") or []
        return [
            _clean({"date": w.get("calendarDate"), "weight_kg": _r(w["weight"] / 1000, 2) if _num(w.get("weight")) else None,
                    "bmi": _r(w.get("bmi")), "body_fat_pct": _r(w.get("bodyFat")),
                    "muscle_mass_kg": _r(w["muscleMass"] / 1000, 1) if _num(w.get("muscleMass")) else None,
                    "source": w.get("sourceType")})
            for w in sorted(rows, key=lambda w: w.get("date") or 0)
        ]
    if metric == "vo2max":
        rows = api.get_max_metrics_range(start, end) or []
        out = []
        for m in rows if isinstance(rows, list) else []:
            g, c = m.get("generic") or {}, m.get("cycling") or {}
            out.append(_clean({"date": g.get("calendarDate") or c.get("calendarDate"),
                               "vo2max": _r(g.get("vo2MaxPreciseValue")), "vo2max_cycling": _r(c.get("vo2MaxPreciseValue"))}))
        return [r for r in out if len(r) > 1]
    raise ValueError(f"Unknown metric {metric!r}")


@garmin_tool("Wellness trend")
def get_wellness_trend(
    metric: Annotated[TrendMetric, Field(description="Which metric to follow over time.")],
    days: Annotated[int, Field(ge=2, le=90, description="Number of days, ending on end_date.")] = 30,
    end_date: Annotated[str | None, Field(description="Last date, YYYY-MM-DD. Defaults to today.")] = None,
) -> dict[str, Any]:
    """Day-by-day values of one wellness metric over up to 90 days, with average,
    min and max. Metrics: sleep, hrv, resting_hr, heart_rate, training_readiness,
    body_battery, stress, steps, intensity_minutes, weight, vo2max."""
    end = _parse_day(end_date, "end_date")
    start = end - timedelta(days=days - 1)
    rows = _trend_rows(_garmin(), metric, start.isoformat(), end.isoformat())
    key = TREND_MAIN_VALUE[metric]
    values = [r.get(key) for r in rows if _num(r.get(key))]
    stats = None
    if values:
        stats = {
            "value": key,
            "average": _avg(values),
            "min": min(values),
            "max": max(values),
            "first": values[0],
            "last": values[-1],
            "days_with_data": len(values),
        }
    return _clean({"metric": metric, "start": start.isoformat(), "end": end.isoformat(), "stats": stats, "days": rows})


if __name__ == "__main__":
    if REMOTE:
        # Stateless JSON responses: every request stands alone, so a machine that was
        # stopped while idle (Fly.io auto-stop) can serve the next request directly.
        mcp.run(
            "streamable-http",
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8080")),
            stateless_http=True,
            json_response=True,
        )
    else:
        mcp.run()
