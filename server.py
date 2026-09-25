"""Garmin MCP server.

Exposes your own Garmin Connect data (sleep, activities, daily stats, HRV)
as tools that Claude or any other MCP client can call.
"""

import os
from datetime import date, timedelta

import garth
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("Garmin")

SESSION_DIR = os.path.join(os.path.dirname(__file__), ".garth")


def get_client():
    """Log in to Garmin Connect, reusing a cached session when possible."""
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        raise ValueError("GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env")

    if os.path.exists(SESSION_DIR):
        garth.resume(SESSION_DIR)
        try:
            garth.client.username  # check that the session is still valid
        except Exception:
            garth.login(email, password)
            garth.save(SESSION_DIR)
    else:
        garth.login(email, password)
        garth.save(SESSION_DIR)

    return garth.client


def _today() -> str:
    return date.today().isoformat()


def _fetch_sleep(day: str) -> dict:
    return garth.connectapi(
        f"/wellness-service/wellness/dailySleepData/{garth.client.username}",
        params={"date": day, "nonSleepBufferMinutes": 60},
    )


@mcp.tool()
def get_sleep_data(date_str: str = None) -> dict:
    """Get sleep data for one date (YYYY-MM-DD). Defaults to today."""
    get_client()
    return _fetch_sleep(date_str or _today())


@mcp.tool()
def get_sleep_last_n_days(days: int = 7) -> list:
    """Get sleep data for the last N days. Defaults to 7."""
    get_client()
    results = []
    for i in range(days):
        day = (date.today() - timedelta(days=i)).isoformat()
        try:
            results.append({"date": day, "data": _fetch_sleep(day)})
        except Exception as e:
            results.append({"date": day, "error": str(e)})
    return results


@mcp.tool()
def get_recent_activities(limit: int = 10) -> list:
    """Get the most recent activities (runs, rides, strength sessions etc.)."""
    get_client()
    activities = garth.connectapi(
        "/activitylist-service/activities/search/activities",
        params={"start": 0, "limit": limit},
    )
    fields = [
        "activityId", "activityName", "startTimeLocal", "duration",
        "distance", "averageHR", "maxHR", "calories",
        "aerobicTrainingEffect", "anaerobicTrainingEffect",
    ]
    return [
        {"type": a.get("activityType", {}).get("typeKey"),
         **{f: a.get(f) for f in fields}}
        for a in activities
    ]


@mcp.tool()
def get_daily_summary(date_str: str = None) -> dict:
    """Get the daily summary for a date (YYYY-MM-DD): steps, resting heart
    rate, stress, body battery and calories. Defaults to today."""
    get_client()
    return garth.connectapi(
        f"/usersummary-service/usersummary/daily/{garth.client.username}",
        params={"calendarDate": date_str or _today()},
    )


@mcp.tool()
def get_hrv(date_str: str = None) -> dict:
    """Get heart rate variability (HRV) for a date (YYYY-MM-DD). Defaults to today."""
    get_client()
    return garth.connectapi(f"/hrv-service/hrv/{date_str or _today()}")


if __name__ == "__main__":
    mcp.run()
