"""Crew scheduling — calendar availability and weather risk."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

from .base import Integration


class CalendarService(Integration):
    """Google Calendar-shaped crew calendar."""

    def _calendar_id(self) -> str:
        return self.credentials.extra.get("calendar_id", "primary")

    def list_events(self, start: date, end: date) -> dict[str, Any]:
        params = {
            "timeMin": f"{start.isoformat()}T00:00:00Z",
            "timeMax": f"{end.isoformat()}T23:59:59Z",
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        return self.request(
            "GET",
            f"/calendars/{self._calendar_id()}/events",
            params=params,
            mock={"items": _mock_busy_days(start, end)},
        )

    def create_event(self, summary: str, start: date, days: int, description: str = "") -> dict[str, Any]:
        end = start + timedelta(days=max(days, 1))
        payload = {
            "summary": summary,
            "description": description,
            # All-day events use exclusive end dates.
            "start": {"date": start.isoformat()},
            "end": {"date": end.isoformat()},
        }
        return self.request(
            "POST",
            f"/calendars/{self._calendar_id()}/events",
            json=payload,
            mock={
                "id": f"evt_{hashlib.sha1(f'{summary}{start}'.encode()).hexdigest()[:10]}",
                "summary": summary,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "status": "confirmed",
            },
        )

    def find_open_days(self, start: date, days_needed: int, horizon_days: int = 30) -> dict[str, Any]:
        """First run of consecutive weekdays with nothing booked."""
        end = start + timedelta(days=horizon_days)
        events = self.list_events(start, end)
        busy = _busy_dates(events)

        run: list[date] = []
        cursor = start
        while cursor <= end:
            is_weekend = cursor.weekday() >= 5
            if not is_weekend and cursor not in busy:
                run.append(cursor)
                if len(run) >= days_needed:
                    return {
                        "found": True,
                        "start_date": run[0].isoformat(),
                        "dates": [d.isoformat() for d in run],
                    }
            else:
                run = []
            cursor += timedelta(days=1)

        return {
            "found": False,
            "reason": f"no {days_needed}-day opening in the next {horizon_days} days",
            "busy_days": sorted(d.isoformat() for d in busy)[:20],
        }


class WeatherService(Integration):
    """Forecast lookup. Exterior work can't be scheduled into rain."""

    def forecast(self, city: str, days: int = 5) -> dict[str, Any]:
        params = {"q": city, "appid": self.credentials.api_key, "units": "imperial"}
        result = self.request(
            "GET",
            "/forecast",
            params=params,
            mock={"list": _mock_forecast(city, days)},
        )
        return {"city": city, "days": days, "raw": result, "summary": _summarize(result, days)}

    def paint_window_risk(self, city: str, start: date, days: int) -> dict[str, Any]:
        """Assess whether an exterior job can run on the given dates."""
        data = self.forecast(city, days=max(days, 5))
        summary = data["summary"]
        wet_days = [d for d in summary if d["rain_chance"] >= 0.4]
        cold_days = [d for d in summary if d["low_f"] < 50]

        if wet_days:
            level = "high"
            advice = f"Rain likely on {', '.join(d['date'] for d in wet_days)} — hold exterior work."
        elif cold_days:
            level = "medium"
            advice = "Overnight lows below 50°F; most exterior coatings won't cure. Start later in the day."
        else:
            level = "low"
            advice = "Forecast is clear enough for exterior work."

        return {
            "city": city,
            "start_date": start.isoformat(),
            "days": days,
            "risk": level,
            "advice": advice,
            "forecast": summary,
        }


# --- deterministic mock helpers ---------------------------------------


def _seed(value: str) -> int:
    return int(hashlib.sha1(value.encode()).hexdigest()[:8], 16)


def _mock_busy_days(start: date, end: date) -> list[dict[str, Any]]:
    """Pseudo-random but stable booked days, so demos are reproducible."""
    items = []
    cursor = start
    while cursor <= end:
        if _seed(cursor.isoformat()) % 5 == 0 and cursor.weekday() < 5:
            items.append(
                {
                    "id": f"evt_busy_{cursor.isoformat()}",
                    "summary": "Existing job",
                    "start": {"date": cursor.isoformat()},
                    "end": {"date": (cursor + timedelta(days=1)).isoformat()},
                }
            )
        cursor += timedelta(days=1)
    return items


def _busy_dates(events: dict[str, Any]) -> set[date]:
    busy: set[date] = set()
    for item in events.get("items", []) or []:
        start = (item.get("start") or {}).get("date") or (item.get("start") or {}).get("dateTime", "")[:10]
        end = (item.get("end") or {}).get("date") or (item.get("end") or {}).get("dateTime", "")[:10]
        if not start:
            continue
        try:
            first = date.fromisoformat(start[:10])
            last = date.fromisoformat(end[:10]) if end else first + timedelta(days=1)
        except ValueError:
            continue
        cursor = first
        while cursor < last:
            busy.add(cursor)
            cursor += timedelta(days=1)
    return busy


def _mock_forecast(city: str, days: int) -> list[dict[str, Any]]:
    base = _seed(city)
    today = date.today()
    entries = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        wobble = (base + offset * 37) % 100
        entries.append(
            {
                "dt_txt": f"{day.isoformat()} 12:00:00",
                "main": {"temp_min": 48 + wobble % 25, "temp_max": 62 + wobble % 20},
                "pop": round((wobble % 70) / 100, 2),
                "weather": [{"main": "Rain" if wobble % 70 >= 40 else "Clear"}],
            }
        )
    return entries


def _summarize(result: dict[str, Any], days: int) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in (result.get("list") or [])[: days * 8]:
        day = str(entry.get("dt_txt", ""))[:10]
        if not day or day in seen:
            continue
        seen.add(day)
        main = entry.get("main", {})
        summary.append(
            {
                "date": day,
                "low_f": round(float(main.get("temp_min", 60))),
                "high_f": round(float(main.get("temp_max", 75))),
                "rain_chance": float(entry.get("pop", 0)),
                "conditions": (entry.get("weather") or [{}])[0].get("main", "Unknown"),
            }
        )
        if len(summary) >= days:
            break
    return summary
