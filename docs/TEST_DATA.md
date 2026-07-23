# Test data for a Home Assistant test instance

A fresh test box has almost no history, so anything that reads the recorder
cannot be tested there without seeding data first. These are the options,
ranked by how well they suit automated, repeatable testing.

## 1. `recorder/import_statistics` (used here)

A native websocket command. No add-on, no CSV, no file access, and the data is
exactly what you specify - which makes assertions possible. This is what the
`ha_data` tools are tested against.

Rules that matter:

- `has_sum` statistics need a **monotonically rising `sum`** (a cumulative
  meter). HA derives `change` per row from it, which is what consumption
  questions actually read. Feed it per-row deltas and every answer is wrong.
- `has_mean` statistics need `mean`, and want `min`/`max` alongside.
- `has_mean` and `has_sum` are mutually exclusive in practice: `state_class:
  measurement` produces the first, `total`/`total_increasing` the second.
- `statistic_id` must be `domain:name` for external statistics (`test:meter`),
  or an existing `entity_id`. External ids are queryable exactly like entities,
  but they are **not** entities: `GetLiveContext` cannot see them, so a model
  will not discover them on its own - name them explicitly when testing.
- `source` must match the id's prefix (`"source": "test"` for `test:meter`).

```python
import asyncio, aiohttp, math
from datetime import datetime, timedelta, timezone

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("http://<host>:8123/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOKEN})
            await ws.receive_json()

            start = (datetime.now(timezone.utc) - timedelta(days=730)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            total, rows = 0.0, []
            for i in range(730):
                total += 1.5 + 1.0 * math.sin(i / 58.0)      # seasonal wave
                rows.append({"start": (start + timedelta(days=i)).isoformat(),
                             "sum": round(total, 4), "state": round(total, 4)})

            await ws.send_json({
                "id": 1, "type": "recorder/import_statistics",
                "metadata": {"has_mean": False, "has_sum": True,
                             "name": "Test Meter", "source": "test",
                             "statistic_id": "test:meter_energy",
                             "unit_of_measurement": "kWh"},
                "stats": rows})
            print((await ws.receive_json())["success"])

asyncio.run(main())
```

Currently loaded on the test box:

| statistic_id | shape | data |
|---|---|---|
| `test:meter_energy` | `has_sum`, kWh | 730 daily rows, ~1.5 kWh/day plus a seasonal wave, total 1095.0015 kWh |
| `test:room_temperature` | `has_mean`, °C | 7 days of hourly mean/min/max, daily sine between 12 and 24 °C |

Removing them again: `recorder/clear_statistics` with the ids.

## 2. CSV import via `klausj1/homeassistant-statistics`

[HACS integration](https://github.com/klausj1/homeassistant-statistics) that
imports and exports long-term statistics as CSV/TSV/JSON. It pairs with the HA
UI's per-entity **Download data** button (History → pick entity and range →
Download data), so a realistic dataset can be lifted out of production and
replayed on the test box.

Use it when you want *real* shapes - solar curves, heating cycles - rather than
synthetic ones. It is a manual, human-in-the-loop path: fine for exploratory
testing, awkward for automated runs.

## 3. Copying the production database

Works, but it is the blunt instrument:

- Stop HA, or call `recorder.disable` first, then copy
  `/config/home-assistant_v2.db`. Copying a live SQLite file without either
  gives a torn database.
- Not officially supported by the recorder integration, though widely done.
- Only applies if production is on SQLite. MariaDB/PostgreSQL installs need a
  dump/restore instead, and the schema is version-locked to the HA release.
- It drags **all** production data - real entity ids, real names, real presence
  history - onto the test box. For a store that also holds personal notes, that
  is a privacy decision, not just a convenience one.

Prefer exporting the handful of entities you actually need (option 2) over
cloning everything.

## 4. Letting the instance generate its own data

Cheapest for state history, useless for long-term statistics on any sensible
timescale: statistics compile every 5 minutes (short-term) and hourly
(long-term), so "last two years" would take two years. Short-term 5-minute
statistics are also purged along with the states (`purge_keep_days`, default
10), which is why old short windows have no fine-grained rows and any tool
reading them has to fall back to coarser periods.

Useful for: `get_history` tests, state-change traces, availability gaps.
Useless for: anything spanning more than a few days.

## Verifying whatever you seeded

Always check answers against the recorder rather than against the model's
phrasing:

```python
await ws.send_json({"id": 1, "type": "recorder/statistics_during_period",
    "start_time": start.isoformat(), "statistic_ids": ["test:meter_energy"],
    "period": "day", "types": ["sum", "change"]})
```

`change` is the per-bucket difference and the number a consumption answer should
match. `sum` is the cumulative counter - never add those up.

`recorder/list_statistic_ids` shows everything available plus its `has_mean` /
`has_sum` flags, which is the fastest way to see what an entity can even be
asked about.
