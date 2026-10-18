# COROS FIT Analyzer — hosted MCP service

This is the cloud-runtime companion for the existing private `coros-fit-analyzer` plugin. The currently uploaded package uses a local stdio process; uploading it to an account does not deploy that process. This service exposes the FIT parser over MCP Streamable HTTP so ChatGPT can call it from a conversation.

## Deploy to Render

1. Put this directory in a Git repository connected to Render, then create a Web Service from the repository using the included `Dockerfile` / `render.yaml`.
2. Use the service's public HTTPS hostname. The MCP endpoint is `https://<service-host>/mcp`; health is `https://<service-host>/healthz`.
3. Once deployed, verify `/healthz`, then call `server_health` and `tools/list` through an MCP client. After the endpoint has been tested, update both `mcp.json` and `.mcp.json` in the private plugin to use that exact endpoint with `type: streamable-http`, bump its version, and publish the plugin update.
4. Only after the endpoint passes a real parser call should the plugin update be treated as live.

## FIT parsing workflow

- Call COROS `queryActivityFitFileDownloadUrls` for the chosen activity, then pass the returned HTTPS URL to `parse_fit_file_url`.
- A raw `.fit` file from COROS or another FIT-producing app/device is also accepted:
  use the binary HTTP upload below, or Base64-encode its bytes for `parse_fit_file_base64`.
  Direct URL fetching remains restricted to the COROS allowlist; it is not a general URL fetcher.
- The server defaults to accepting only `*.coros.com` HTTPS downloads and rejects non-public DNS destinations. If COROS changes its download domain or redirects to a separate CDN, add only the observed trusted suffix to `COROS_ALLOWED_HOST_SUFFIXES` in the service environment.
- The initial parse returns the actual FIT message counts, record count, timestamp coverage, sampling gaps, field inventory, summaries, and a dataset ID. It includes a short first/last-row preview rather than tens of thousands of records in one response.
- Use `get_fit_record_chunk(dataset_id, start_index, count)` to inspect raw record rows; count is capped at 500. The server keeps parsed rows in memory for 20 minutes; after a restart/expiry, parse again.
- `export_fit_gpx` and `export_fit_kml` convert the parsed track to GPX 1.1 or KML from the original coordinates. The same files are available at `/datasets/{dataset_id}/track.gpx` and `/datasets/{dataset_id}/track.kml` while the dataset is cached. `stride` thins points when the track is very large.
- `export_fit_original(dataset_id)` returns JSON describing an unchanged original FIT download,
  including byte count, SHA-256, relative `download_path` and remaining cache lifetime.
  GET that path to obtain the binary file for import into another app. GPX/KML conversions
  are not replacements for the original FIT: they cannot preserve every FIT message.
- `analyze_climb` totals ascent and descent from recorded altitude, using a 3 m hysteresis and listing climb/descent segments of at least 10 m. Missing altitudes are not interpolated.
- `get_track_location_weather` returns a track position (last valid GPS point by default, or `position=start` / `record_index`) and queries Open-Meteo for that coordinate on the point's local date. Only the latitude, longitude, and date leave the service.

## Why URL parsing exists

A FIT file around 580 KiB becomes roughly 775 KiB in Base64. Passing that as text through a model call is wasteful and can exceed practical request/context limits. The server fetches the signed COROS download URL itself, checks the response size and FIT CRC, then returns actual parse metadata and paged records.

## Security notes

- No FIT files are persisted to disk. Original FIT bytes and decoded records are held in
  process memory temporarily, with the same 20-minute TTL and four-dataset capacity.
  Weather lookup sends only the selected latitude, longitude, and date to Open-Meteo;
  the FIT file itself is not sent to the weather provider.
- File downloads are HTTPS-only and domain-allowlisted by default; redirects are revalidated.
- The service is a public MCP endpoint without user-specific authentication in this draft. Before broadly sharing it or placing sensitive tools on the same host, configure host-level access control / OAuth. Keep it private and only expose the FIT parsing functions.
- The 80 MiB input limit and 500-record page limit are enforced server-side.

## Local validation

Run `python -m py_compile server.py` for syntax validation. Full runtime tests require installing packages from `requirements.txt`; the current ChatGPT analysis container may not have Internet access for those packages.

## Deterministic activity facts

After parsing, call `analyze_activity(dataset_id, ...)`. This adds a separate
`activity_facts.py` layer and leaves the existing parsing/export/climb tools available.
The response is factual JSON, not a coaching report:

- `quality_report`: field coverage, valid counts, field sampling intervals, timestamp
  gaps/duplicates/reversals, GPS/altitude/heart-rate anomalies, distance/speed conflicts,
  explicit timer pauses, and device versus computed ascent.
- `terrain_segments`: uninterrupted natural climbs/descents with a 3 m altitude anchor
  and at least 10 m net change, distance/time, pace, weighted heart rate and dynamics,
  GPS horizontal and FIT-distance grades, and uphill VAM. Invalid intervals break segments.
- Supplementary 1 km / 5 km bins use original sample boundaries without interpolation.
  An interval crossing a distance boundary belongs entirely to its starting bin; actual
  indexes and measured distances are returned. These are approximate distance bins.
- Grade classes are flat below 3%, gentle 3–8%, moderate 8–15%, steep at least 15%,
  separately for up/down. Half comparisons use the activity UTC time midpoint. Adjacent
  GPS/altitude differences can be noisy; these bins are not assessments of technical terrain.
- `hr_dynamics`: optional explicit personal zone boundaries, time in zones, and conservative
  comparable-grade/speed heart-rate change candidates. No age formula is used.
- `running_dynamics`: only recorded FIT fields, their availability and statistics. Native
  cadence and stride units are retained; cadence is never automatically doubled.
- `fatigue_signals`: grade-class pace/dynamics changes, labeled C with no causal explanation.
- `training_gate`: unresolved or unknown pain restricts stage candidates to A/B and prohibits
  intervals, hill sprints, plyometrics and heavy downhill. Resolved pain does not auto-approve
  progression or assign a stage. This is an input gate, not a diagnosis or training prescription.

Optional arguments:

```python
analyze_activity(
    dataset_id="<parsed dataset id>",
    hr_zone_boundaries=[100, 130, 150, 170, 190],
    hr_zone_source="user_explicit",
    pain_status="unresolved",
    requested_stage="B",
    nutrition_log=[{
        "timestamp": "2026-01-01T00:30:00Z",
        "carbohydrate_g": 30,
        "water_ml": 250,
    }],
    previous_dataset_id=None,
    include_weather=False,
)
```

The numbers above illustrate the input shape, not recommended personal zones or intake.
Zone sources accept `user_explicit`, `user_max_hr`, or `user_lthr`; pass actual boundaries
in bpm rather than a maximum heart rate alone. Nutrition uses elapsed hours; omitted
amounts remain null. An explicitly empty log means no reported intake.

Weather is opt-in and sends at most six sampled coordinates and their dates to Open-Meteo.
Samples span activity UTC time and report nearest-hour alignment error, temperature,
humidity, wind and precipitation. An error over one hour is rejected. Failures return
unverified with no invented values. Grid weather is an external estimate, not wrist-sensor
or on-trail observation. Altitude-band residence is reported for observed valid intervals.

`previous_dataset_id` enables a 30 m spatial corridor check with overlap fraction and
alignment errors. This version intentionally returns alignment only: performance progression
remains blocked even when sample overlap reaches 80%, because grade/load/environment are
not yet aligned. Overlap is sample-based, not distance-weighted. Heart-rate recovery,
run/walk classification and cadence–stride speed consistency also remain explicitly
unsupported or blocked until their additional assumptions are established.

All averages use left-end samples weighted by valid adjacent interval durations. Missing
values are null; time gaps are never bridged. Paused intervals do not contribute movement
metrics. Without timer events, `active_seconds` is observed time only and is marked as
movement-unconfirmed. Gaps are excluded, so this need not equal device timer duration.
Natural-segment altitude/VAM uses the processed hysteresis anchor; charts must identify it
as processed. Raw chart sequences come from `get_fit_record_chunk`; no separate chart
calculation or rendered chart is introduced here. Segment and grade-group VAM methods
are distinct and documented in response metadata. Evidence A applies only to qualified
raw-field availability, B to derived facts, and C to candidate changes.

A compact instruction for the reporting model:

> Only explain the supplied activity-facts JSON. Never invent missing fields or recompute
> measurements. Cite the JSON paths and label every conclusion A–D without upgrading
> candidate signals. Null or blocked means unavailable, not zero. Preserve reported
> methods, support durations and uncertainty. Do not infer injury, technical terrain,
> fatigue causes or historical progress from unsupported data. Respect training_gate;
> never prescribe prohibited sessions or automatically advance stages. Write coaching
> interpretation separately from measured facts.

Validation: run `python -m unittest discover -p 'test_activity_facts.py'` and
`python -m py_compile server.py activity_facts.py`. Tests use synthetic original records
and do not contact COROS or weather services. Real FIT/network/deployed-service validation
is separate.

## Raw FIT interchange for other apps (JSON API)

`POST /analyze/fit?file_name=activity.fit` accepts the original binary request body,
with `Content-Type: application/octet-stream` or `application/vnd.ant.fit`.
It does not accept multipart form data or a JSON/Base64 body at this endpoint.
The maximum body size is 80 MiB; invalid FIT/CRC returns HTTP 400, oversized uploads
413, and unsupported media types 415. Successful responses are `application/json`:

```json
{
  "schema_version": "1.0",
  "parse": {"dataset_id": "<id>", "record_count": 2},
  "facts": {"schema_version": "1.0", "dataset_id": "<id>"},
  "original_fit": {
    "status": "success",
    "format": "fit",
    "file_name": "activity.fit",
    "content_type": "application/vnd.ant.fit",
    "file_bytes": 44,
    "sha256": "<SHA-256 of original bytes>",
    "original_bytes_preserved": true,
    "download_path": "/datasets/<id>/original.fit",
    "expires_in_seconds": 1199
  }
}
```

This example abbreviates `parse` and `facts`; placeholder values are illustrative.
The binary-upload endpoint uses default fact options: no external weather lookup,
no personal HR zones, unknown pain state, and no nutrition/history input.
Use the returned dataset ID with MCP `analyze_activity` for those optional inputs.

Example app integration:

```python
from pathlib import Path
from urllib.parse import urljoin
import hashlib
import requests

base_url = "https://<your-service-host>"
with Path("activity.fit").open("rb") as source:
    response = requests.post(
        base_url + "/analyze/fit",
        params={"file_name": "activity.fit"},
        data=source,
        headers={"Content-Type": "application/octet-stream"},
        timeout=120,
    )
response.raise_for_status()
result = response.json()
facts = result["facts"]
original = result["original_fit"]
download = requests.get(urljoin(base_url, original["download_path"]), timeout=120)
download.raise_for_status()
assert hashlib.sha256(download.content).hexdigest() == original["sha256"]
Path("original.fit").write_bytes(download.content)
```

`download_path` must be resolved against the actual service origin; no public hostname
is guessed from request headers. The tool and upload responses are JSON; the original-file
GET response is binary FIT, with attachment headers. No Base64 file is embedded in result JSON.
Downloads expire with their dataset and can become unavailable earlier on eviction/restart;
missing/expired originals return HTTP 404 JSON. Download paths inherit the service's existing
access-control model and should only be shared with intended recipients. A receiving app
must support FIT import; the service cannot guarantee that every app supports every FIT field.

The revised Chinese model prompt is in `PROMPT.txt`. It covers raw FIT, COROS URL and
existing dataset inputs, the JSON response boundary, evidence, original-file export, and
unsupported analyses. It is for the reporting model, not a replacement for API validation.

Run all local tests with `python -m unittest discover -p 'test_*.py'` after installing
`requirements.txt` plus the HTTP test dependency `httpx`. Interchange tests construct a
minimal FIT fixture with a valid CRC, verify byte-for-byte download and SHA-256, and
exercise upload/media/size/expiry errors without contacting an external provider.
