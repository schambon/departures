import copy
import json
import os
import sys
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import departures as d  # noqa: E402

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)  # 13:00 in Paris
LINE = "STIF:Line::C01742:"
NORTH = ["STIF:StopPoint:Q:990001:"]


def load(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


class DepartureTests(unittest.TestCase):
    def setUp(self):
        self.payload = load("stop_monitoring.json")

    def test_quay_filter_keeps_one_direction(self):
        deps = d.parse_departures(self.payload, NOW, LINE, NORTH)
        self.assertEqual(len(deps), 12)
        self.assertTrue(all(x["destination"] == "Terminus Nord" for x in deps))
        self.assertTrue(all(x["platform"] == "2C" for x in deps))

    def test_times_are_paris_time_and_minutes(self):
        first = d.parse_departures(self.payload, NOW, LINE, NORTH)[0]
        self.assertEqual(first["time"], "13:04")  # 12:04:46Z
        self.assertEqual(first["minutes"], 4)

    def test_delay_detected_from_times_although_status_says_on_time(self):
        # Real data: aimed 12:09:10Z, expected 12:17:54Z, DepartureStatus "onTime".
        deps = d.parse_departures(self.payload, NOW, LINE, ["STIF:StopPoint:Q:990002:"])
        late = deps[0]
        self.assertEqual(late["status"], "onTime")
        self.assertTrue(late["delayed"])
        self.assertEqual(late["delay_minutes"], 8)
        self.assertIn("DELAYED +8 min", d.render("X", [], deps, 6))

    def test_on_time_train_is_not_flagged(self):
        deps = d.parse_departures(self.payload, NOW, LINE, NORTH)
        self.assertFalse(deps[1]["delayed"])
        self.assertNotIn("DELAYED", d.render("X", [], deps[1:2], 6))

    def test_cancelled_train(self):
        # Synthetic: no cancelled train in the real capture. "cancelled" is the SIRI standard value.
        p = copy.deepcopy(self.payload)
        visit = p["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"][0]["MonitoredStopVisit"][0]
        visit["MonitoredVehicleJourney"]["MonitoredCall"]["DepartureStatus"] = "cancelled"
        deps = d.parse_departures(p, NOW, LINE, NORTH)
        self.assertTrue(deps[0]["cancelled"])
        self.assertFalse(deps[0]["delayed"])
        self.assertIn("CANCELLED", d.render("X", [], deps, 6))

    def test_past_departures_are_dropped(self):
        later = datetime(2026, 11, 7, 12, 10, 0, tzinfo=timezone.utc)
        deps = d.parse_departures(self.payload, later, LINE, NORTH)
        self.assertEqual(deps[0]["time"], "13:18")

    def test_empty_response(self):
        p = {"Siri": {"ServiceDelivery": {"StopMonitoringDelivery": [{"MonitoredStopVisit": []}]}}}
        self.assertEqual(d.parse_departures(p, NOW), [])
        self.assertIn("No upcoming departures", d.render("X", [], [], 6))

    def test_unexpected_layout(self):
        with self.assertRaises(d.ToolError):
            d.parse_departures({}, NOW)

    def test_error_condition(self):
        p = {"Siri": {"ServiceDelivery": {"StopMonitoringDelivery": [
            {"ErrorCondition": {"ErrorInformation": {"ErrorText": "boom"}}}]}}}
        with self.assertRaises(d.ToolError):
            d.parse_departures(p, NOW)


class DisruptionTests(unittest.TestCase):
    def test_active_message_uses_short_text(self):
        # Real message, captured for metro line 1 (RER D had none at capture time).
        msgs = d.parse_disruptions(load("general_message_line1.json"), NOW, "STIF:Line::C01371:")
        self.assertEqual(len(msgs), 1)
        self.assertIn("Châtelet", msgs[0]["text"])
        self.assertEqual(msgs[0]["channel"], "Perturbation")

    def test_expired_message_is_dropped(self):
        later = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.assertEqual(d.parse_disruptions(load("general_message_line1.json"), later), [])

    def test_other_line_is_filtered_out(self):
        self.assertEqual(d.parse_disruptions(load("general_message_line1.json"), NOW, LINE), [])


class ServerTests(unittest.TestCase):
    def start(self, fetch):
        settings = {"station": "Test", "line": LINE, "quays": NORTH, "count": 3}
        self.service = d.BoardService(fetch, settings, ttl=60)
        self.httpd = d.ThreadingHTTPServer(("127.0.0.1", 0), d.make_handler(self.service))
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_page_and_json_and_cache(self):
        calls = []
        sm = load("stop_monitoring.json")
        # fixture data is from 7 Nov 2026; make it "now" so departures are not filtered out
        self.start(lambda: calls.append(1) or (sm, None))
        with unittest.mock.patch.object(d, "datetime", wraps=datetime) as dt:
            dt.now.return_value = NOW
            code, body = self.get("/next.json")
            self.get("/next.json")
        self.assertEqual(code, 200)
        board = json.loads(body)
        self.assertEqual(len(board["departures"]), 3)
        self.assertEqual(len(calls), 1)  # second request served from cache
        code, html = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("next.json", html)

    def test_next_txt_is_plain_text_and_errors_are_502(self):
        sm = load("stop_monitoring.json")
        self.start(lambda: (sm, None))
        with unittest.mock.patch.object(d, "datetime", wraps=datetime) as dt:
            dt.now.return_value = NOW
            code, body = self.get("/next.txt")
        self.assertEqual(code, 200)
        self.assertIn("13:04", body)
        self.assertIn("Terminus Nord", body)
        self.assertEqual(len(body.strip().splitlines()), 5)  # station, blank, 3 departures

    def test_next_txt_error(self):
        def boom():
            raise d.ToolError("Network failure: down")
        self.start(boom)
        code, body = self.get("/next.txt")
        self.assertEqual(code, 502)
        self.assertEqual(body, "Error: Network failure: down")

    def test_api_error_gives_502_json(self):
        def boom():
            raise d.ToolError("API quota reached (HTTP 429). Try again later.")
        self.start(boom)
        code, body = self.get("/next.json")
        self.assertEqual(code, 502)
        self.assertIn("quota", json.loads(body)["error"])

    def test_unknown_path_is_404(self):
        self.start(lambda: (None, None))
        self.assertEqual(self.get("/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
