# departures.py

Next departures from one Île-de-France station, plus active disruption messages for your line.
Python 3.9+, standard library only (no `requests`).

```
Your station (RER X, towards Y)

13:04    3 min  Some Terminus   pl. 2C
13:19   17 min  Some Terminus   pl. 2C
```

## Setup

1. Generate an API token on [PRIM](https://prim.iledefrance-mobilites.fr) ("mon jeton d'API").
2. `export PRIM_API_KEY=...` (never put it in a file in this folder).
3. Create your config: `cp config.example.json config.json`, then edit it. The example is set for Châtelet - Les Halles on
   RER A; see "Finding your station" below to use another station or pick a direction. `config.json` is git-ignored,
   so your own station stays out of the repository.

## Usage

```
python3 departures.py                     # uses config.json
python3 departures.py --json              # normalised JSON, for scripts / iOS Shortcuts
python3 departures.py --count 10
python3 departures.py --ref STIF:StopArea:SP:45102: --line STIF:Line::C01742: --quay STIF:StopPoint:Q:<id>:
python3 departures.py --find-stop "Châtelet"
python3 departures.py --serve             # web page on http://127.0.0.1:8080/
python3 -m unittest discover -s tests
```

### Web page (`--serve`)

`python3 departures.py --serve [--host ADDR] [--port N]` serves:

- `/`: a mobile-friendly page that refreshes every 30 s (minutes are recomputed in the browser every 10 s). On iPhone, Safari → Share → Add to Home Screen.
- `/next.json`: the same data as `--json` (HTTP 502 with `{"error": ...}` if the PRIM call fails).
- `/next.txt`: the same text as the command-line output, for an iOS Shortcut ("Get Contents of URL" then "Show Notification"). HTTP 502 with `Error: ...` on failure.

The PRIM responses are cached for 20 s, so any number of refreshes costs at most 3 requests × 2 calls a minute against the daily quota.
There is **no authentication**: the default bind address is `127.0.0.1`. Only bind to a LAN or Tailscale address if you trust everything that can reach it.
The page's JavaScript was syntax-checked and the endpoints tested, but the page has not been tried in a real browser.

Delayed trains show `DELAYED +N min`, cancelled ones `CANCELLED`. Disruptions appear above the list, prefixed `!`.
Exit code is 1 with a one-line message on stderr for: missing/bad token (401/403), quota (429), network failure, empty or malformed response.

`--json` prints the tool's normalised structure (`station`, `generated_at`, `disruptions`, `departures`), not the raw SIRI payload.

### Config keys

| key | meaning |
|---|---|
| `monitoring_ref` | `STIF:StopArea:SP:<zdaid>:` for the station |
| `line_ref` | `STIF:Line::<id>:`; filters departures and selects the disruption messages |
| `quays` | optional list of `STIF:StopPoint:Q:<id>:`; keeps one direction/platform |
| `station`, `count` | display name, number of departures (default 6) |

### Finding your station

`--find-stop NAME` searches the IDFM `arrets` dataset on data.iledefrance-mobilites.fr (open, no token) and prints each
stop area with its `monitoring_ref` and its StopPoints. Pick the `[rail]` entry. To work out which StopPoint is which
direction, run once without `quays` and look at `quay` and `destination` in `--json`.
Line ids come from the `referentiel-des-lignes` dataset (RER A = `C01742`).

Example: Châtelet - Les Halles is stop area 45102 and RER A is line `C01742`, which is what `config.example.json` uses.
Its `quays` filter keeps platform 2A (`473921`), the westbound trains towards Saint-Germain-en-Laye, Poissy and Cergy; platform 1A (`473993`) is eastbound.

## Verified against the live API (7 Nov 2026)

- Auth: header `apikey: <token>` returns 200; a bad token returns 401.
- Next departures: `GET https://prim.iledefrance-mobilites.fr/marketplace/stop-monitoring?MonitoringRef=...`
- Messages: `GET https://prim.iledefrance-mobilites.fr/marketplace/general-message?LineRef=STIF:Line::C01742:`
  (without `LineRef` it returns 400).
- Response fields used: `MonitoredCall.AimedDepartureTime`, `ExpectedDepartureTime`, `DepartureStatus`,
  `DeparturePlatformName`, `DestinationDisplay`, `DepartureStopAssignment.ExpectedQuayRef`, `MonitoredVehicleJourney.LineRef`;
  messages: `InfoMessage[].Content.Message[]` (`SHORT_MESSAGE` / `TEXT_ONLY`), `Content.LineRef`, `ValidUntilTime`.
- Quota headers show a limit of **1000 requests/day** for this token (`x-ratelimit-limit-day`). Each run costs 2 calls
  (1 if no `line_ref`), so roughly 500 runs a day. Older tokens may have higher limits (per PRIM's Info Trafic page).

## Known limitations / not verified

- **Cancelled trains:** none occurred during testing. The tool treats `DepartureStatus == "cancelled"` (the SIRI standard value)
  as cancelled; the real value used by IDFM is unconfirmed. The test for it uses a synthetic edit of the real fixture.
- **Status field is unreliable:** a train 8 minutes late was still reported as `"onTime"`, so delay is computed from
  expected vs aimed time (threshold 60 s) and the status is only used for cancellation.
- **StopPoint-level queries returned nothing:** `MonitoringRef=STIF:StopPoint:Q:<id>:` gave an empty list for every quay
  of the test station, while the stop area form worked and its results name those same quays. Cause unknown, hence the quay filter is applied client-side.
- **Disruptions:** The test line had no active message at the time, so the parser was checked on a real message captured for metro
  line 1 (`tests/fixtures/general_message_line1.json`). Only `ValidUntilTime` was seen; no start time or severity field was observed.
  Only the `Perturbation` channel appeared in the messages sampled.
- **429 handling** and the per-second rate limit were not exercised.
- PRIM's documentation pages require a login and could not be read; everything above comes from live responses.
- Terminus trains and trains with no real-time data may lack expected times; the tool then falls back to the aimed time.
- The next-departures API only returns what the operator publishes (about 12 trains per direction at capture time).

## Files

- `departures.py`: the tool
- `config.example.json`: example station settings; copy to `config.json` (git-ignored) and edit
- `tests/`: unit tests and the real fixtures (`stop_monitoring.json`, a real capture with station, line, quay and destination names replaced by placeholders)
