#!/usr/bin/env python3
"""Next departures from one Île-de-France station, via the PRIM (IDFM) SIRI Lite APIs.

Token: environment variable PRIM_API_KEY (never stored in this repo).
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

PRIM_BASE = "https://prim.iledefrance-mobilites.fr/marketplace"
STOP_MONITORING_URL = PRIM_BASE + "/stop-monitoring"
GENERAL_MESSAGE_URL = PRIM_BASE + "/general-message"
REFERENTIAL_URL = (
    "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets/arrets/records"
)
PARIS = ZoneInfo("Europe/Paris")
DELAY_THRESHOLD_S = 60
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class ToolError(Exception):
    """An error whose message is safe to print as-is."""


# --- HTTP ------------------------------------------------------------------

def http_get_json(url, params, api_key=None, timeout=10):
    full = url + "?" + urllib.parse.urlencode(params, safe=":")
    headers = {"Accept": "application/json", "User-Agent": "departures-cli/1.0"}
    if api_key:
        headers["apikey"] = api_key
    try:
        with urllib.request.urlopen(urllib.request.Request(full, headers=headers), timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ToolError(f"The API rejected the token (HTTP {e.code}). Check PRIM_API_KEY.")
        if e.code == 429:
            raise ToolError("API quota reached (HTTP 429). Try again later.")
        raise ToolError(f"The API returned an error (HTTP {e.code}).")
    except urllib.error.URLError as e:
        raise ToolError(f"Network failure: {e.reason}")
    except (TimeoutError, OSError) as e:
        raise ToolError(f"Network failure: {e}")
    if not body.strip():
        raise ToolError("The API returned an empty response.")
    try:
        return json.loads(body)
    except ValueError:
        raise ToolError("The API returned a response that is not valid JSON.")


def _delivery(payload, key):
    try:
        return payload["Siri"]["ServiceDelivery"][key][0]
    except (KeyError, IndexError, TypeError):
        raise ToolError("Unexpected response layout from the API.")


def _check_error(delivery):
    err = delivery.get("ErrorCondition")
    if err:
        info = err.get("ErrorInformation", {})
        raise ToolError("API error: " + (info.get("ErrorText") or info.get("ErrorDescription") or "unknown"))


# --- parsing ---------------------------------------------------------------

def parse_time(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _value(x):
    """SIRI Lite wraps many strings as [{"value": ...}] or {"value": ...}."""
    if isinstance(x, list):
        x = x[0] if x else None
    if isinstance(x, dict):
        x = x.get("value")
    return x


def parse_departures(payload, now, line_ref=None, quays=None):
    """Return normalised departures, soonest first, ignoring those already gone."""
    delivery = _delivery(payload, "StopMonitoringDelivery")
    _check_error(delivery)
    out = []
    for visit in delivery.get("MonitoredStopVisit", []):
        mvj = visit.get("MonitoredVehicleJourney", {})
        call = mvj.get("MonitoredCall", {})
        if line_ref and _value(mvj.get("LineRef")) != line_ref:
            continue
        quay = _value((call.get("DepartureStopAssignment") or call.get("ArrivalStopAssignment") or {}).get("ExpectedQuayRef"))
        if quays and quay not in quays:
            continue
        aimed = parse_time(call.get("AimedDepartureTime") or call.get("AimedArrivalTime"))
        expected = parse_time(call.get("ExpectedDepartureTime") or call.get("ExpectedArrivalTime")) or aimed
        if expected is None:
            continue
        status = call.get("DepartureStatus") or call.get("ArrivalStatus")
        cancelled = status == "cancelled"
        delay_s = int((expected - aimed).total_seconds()) if aimed else 0
        # The status field says "onTime" even for trains 8 minutes late, so compare times.
        delayed = not cancelled and delay_s >= DELAY_THRESHOLD_S
        if (expected - now).total_seconds() < 0:
            continue
        out.append({
            "time": expected.astimezone(PARIS).strftime("%H:%M"),
            "expected": expected.isoformat(),
            "aimed": aimed.isoformat() if aimed else None,
            "minutes": int((expected - now).total_seconds() // 60),
            "delay_minutes": delay_s // 60 if delayed else 0,
            "delayed": delayed,
            "cancelled": cancelled,
            "status": status,
            "destination": _value(call.get("DestinationDisplay")) or _value(mvj.get("DestinationName")),
            "platform": _value(call.get("DeparturePlatformName")) or _value(call.get("ArrivalPlatformName")),
            "line_ref": _value(mvj.get("LineRef")),
            "quay": quay,
        })
    out.sort(key=lambda d: d["expected"])
    return out


def parse_disruptions(payload, now, line_ref=None):
    delivery = _delivery(payload, "GeneralMessageDelivery")
    _check_error(delivery)
    out = []
    for msg in delivery.get("InfoMessage", []):
        until = parse_time(msg.get("ValidUntilTime"))
        if until and until < now:
            continue
        content = msg.get("Content", {})
        lines = [_value(l) for l in content.get("LineRef", [])]
        if line_ref and lines and line_ref not in lines:
            continue
        texts = {m.get("MessageType"): _value(m.get("MessageText")) for m in content.get("Message", [])}
        text = texts.get("SHORT_MESSAGE") or texts.get("TEXT_ONLY") or next(iter(texts.values()), None)
        if text:
            out.append({
                "channel": _value(msg.get("InfoChannelRef")),
                "text": text,
                "valid_until": until.isoformat() if until else None,
            })
    return out


# --- rendering -------------------------------------------------------------

def render(station, disruptions, departures, count):
    lines = [station, ""]
    for d in disruptions:
        lines.append(f"! {d['text']}")
    if disruptions:
        lines.append("")
    if not departures:
        lines.append("No upcoming departures found.")
    for d in departures[:count]:
        if d["cancelled"]:
            marker = "  CANCELLED"
        elif d["delayed"]:
            marker = f"  DELAYED +{d['delay_minutes']} min"
        else:
            marker = ""
        plat = f"  pl. {d['platform']}" if d["platform"] else ""
        lines.append(f"{d['time']}  {d['minutes']:>3} min  {d['destination'] or '?':<28}{plat}{marker}")
    return "\n".join(lines)


# --- stop search -----------------------------------------------------------

def find_stop(name):
    where = 'search(arrname, "%s")' % name.replace('"', " ")
    payload = http_get_json(REFERENTIAL_URL, {"where": where, "limit": 100,
                                              "select": "arrid,arrname,arrtype,arrtown,zdaid"})
    zones = {}
    for r in payload.get("results", []):
        z = zones.setdefault(r["zdaid"], {"name": r["arrname"], "town": r["arrtown"], "types": set(), "quays": []})
        z["types"].add(r["arrtype"])
        z["quays"].append(r["arrid"])
    return zones


def render_stops(zones):
    if not zones:
        return "No stop found."
    lines = []
    for zid, z in sorted(zones.items(), key=lambda kv: ("rail" not in kv[1]["types"], kv[1]["name"])):
        lines.append(f"{z['name']} ({z['town']}) [{', '.join(sorted(z['types']))}]")
        lines.append(f"  monitoring_ref: STIF:StopArea:SP:{zid}:")
        lines.append("  quays:          " + ", ".join(f"STIF:StopPoint:Q:{q}:" for q in z["quays"]))
    return "\n".join(lines)


# --- board (shared by CLI and server) ----------------------------------------

def fetch_payloads(ref, line, key):
    sm = http_get_json(STOP_MONITORING_URL, {"MonitoringRef": ref}, key)
    gm = http_get_json(GENERAL_MESSAGE_URL, {"LineRef": line}, key) if line else None
    return sm, gm


def build_board(sm, gm, now, station, line, quays, count):
    departures = parse_departures(sm, now, line, quays)
    disruptions = parse_disruptions(gm, now, line) if gm is not None else []
    return {"station": station, "generated_at": now.isoformat(),
            "disruptions": disruptions, "departures": departures[:count]}


# --- web server ------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Departures</title>
<style>
:root{--bg:#fff;--fg:#111;--mute:#666;--warn:#b45309;--bad:#b91c1c;--line:#ddd}
@media(prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mute:#999;--warn:#fbbf24;--bad:#f87171;--line:#333}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:17px/1.4 -apple-system,system-ui,sans-serif;max-width:560px}
h1{font-size:18px;margin:0 0 12px}
.msg{border-left:4px solid var(--warn);padding:4px 10px;margin:0 0 10px;font-size:15px}
.row{display:grid;grid-template-columns:3.4em 3.6em 1fr auto;gap:8px;padding:10px 0;border-top:1px solid var(--line);align-items:baseline}
.t{font-weight:600;font-variant-numeric:tabular-nums}.m{color:var(--mute);font-variant-numeric:tabular-nums}
.p{color:var(--mute);font-size:14px}.late{color:var(--warn);font-weight:600}.cx{color:var(--bad);font-weight:600}
.cx-row .d{text-decoration:line-through}
#foot{color:var(--mute);font-size:13px;margin-top:12px}
</style></head><body>
<h1 id="st">&nbsp;</h1><div id="msgs"></div><div id="rows"></div><div id="foot"></div>
<script>
const $=id=>document.getElementById(id);
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
let board=null;
function draw(){
  if(!board)return;
  $('st').textContent=board.station;
  const msgs=$('msgs');msgs.replaceChildren(...board.disruptions.map(d=>el('div','msg',d.text)));
  const now=Date.now(),rows=$('rows');
  const deps=board.departures.filter(d=>Date.parse(d.expected)>=now-30000);
  rows.replaceChildren(...(deps.length?deps.map(d=>{
    const r=el('div','row'+(d.cancelled?' cx-row':''));
    r.append(el('span','t',d.time),
      el('span','m',Math.max(0,Math.floor((Date.parse(d.expected)-now)/60000))+' min'),
      el('span','d',d.destination||'?'));
    const side=el('span','p',d.platform?'pl. '+d.platform:'');
    if(d.cancelled)side.append(el('div','cx','CANCELLED'));
    else if(d.delayed)side.append(el('div','late','+'+d.delay_minutes+' min'));
    r.append(side);return r}):[el('div','p','No upcoming departures.')]));
}
async function load(){
  try{
    const r=await fetch('next.json',{cache:'no-store'});const j=await r.json();
    if(!r.ok)throw new Error(j.error||('HTTP '+r.status));
    board=j;draw();$('foot').textContent='Updated '+new Date().toLocaleTimeString('fr-FR',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Paris'});
  }catch(e){$('foot').textContent='Error: '+e.message}
}
load();setInterval(load,30000);setInterval(draw,10000);
</script></body></html>
"""


class BoardService:
    """Caches the PRIM payloads for a few seconds so refreshes don't burn the daily quota."""

    def __init__(self, fetch, settings, ttl=20):
        self.fetch, self.settings, self.ttl = fetch, settings, ttl
        self._lock = threading.Lock()
        self._at = None
        self._payloads = None

    def board(self):
        with self._lock:
            if self._payloads is None or time.monotonic() - self._at > self.ttl:
                self._payloads = self.fetch()
                self._at = time.monotonic()
            sm, gm = self._payloads
        s = self.settings
        return build_board(sm, gm, datetime.now(timezone.utc), s["station"], s["line"], s["quays"], s["count"])


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/next.json":
                try:
                    self._send(200, json.dumps(service.board(), ensure_ascii=False), "application/json")
                except ToolError as e:
                    self._send(502, json.dumps({"error": str(e)}), "application/json")
            elif path == "/next.txt":
                try:
                    b = service.board()
                    self._send(200, render(b["station"], b["disruptions"], b["departures"], len(b["departures"]) or 1),
                               "text/plain; charset=utf-8")
                except ToolError as e:
                    self._send(502, f"Error: {e}", "text/plain; charset=utf-8")
            else:
                self._send(404, "Not found", "text/plain; charset=utf-8")

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    return Handler


def serve(host, port, service):
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"Serving on http://{host}:{httpd.server_address[1]}/  (Ctrl-C to stop)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# --- main ------------------------------------------------------------------

def load_config(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ToolError(f"Cannot read config file {path}: {e}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Next departures from one Île-de-France station.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="config file (default: config.json beside the script)")
    p.add_argument("--station", help="display name")
    p.add_argument("--ref", help="MonitoringRef, e.g. STIF:StopArea:SP:99001:")
    p.add_argument("--line", help="line filter, e.g. STIF:Line::C01742:")
    p.add_argument("--quay", action="append", help="only this StopPoint (repeatable)")
    p.add_argument("--count", type=int, help="number of departures (default 6)")
    p.add_argument("--json", action="store_true", help="print normalised JSON instead of text")
    p.add_argument("--find-stop", metavar="NAME", help="search the stops referential and exit")
    p.add_argument("--serve", action="store_true", help="serve a web page and /next.json instead of printing")
    p.add_argument("--host", default="127.0.0.1", help="address to bind with --serve (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="port with --serve (default 8080)")
    args = p.parse_args(argv)

    try:
        if args.find_stop:
            print(render_stops(find_stop(args.find_stop)))
            return 0

        cfg = load_config(args.config)
        ref = args.ref or cfg.get("monitoring_ref")
        if not ref:
            raise ToolError("No stop configured. Set monitoring_ref in config.json, or use --ref. Try --find-stop NAME.")
        station = args.station or cfg.get("station") or ref
        line = args.line or cfg.get("line_ref")
        quays = args.quay or cfg.get("quays")
        count = args.count or cfg.get("count") or 6
        key = os.environ.get("PRIM_API_KEY")
        if not key:
            raise ToolError("PRIM_API_KEY is not set.")

        if args.serve:
            service = BoardService(lambda: fetch_payloads(ref, line, key),
                                   {"station": station, "line": line, "quays": quays, "count": count})
            try:
                serve(args.host, args.port, service)
            except OSError as e:
                raise ToolError(f"Cannot start the server on {args.host}:{args.port}: {e}")
            return 0

        sm, gm = fetch_payloads(ref, line, key)
        board = build_board(sm, gm, datetime.now(timezone.utc), station, line, quays, count)
        if args.json:
            print(json.dumps(board, ensure_ascii=False, indent=2))
        else:
            print(render(station, board["disruptions"], board["departures"], count))
        return 0
    except ToolError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
