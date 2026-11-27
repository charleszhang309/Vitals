"""监控、访问校验和 Windows 文件流的回归测试。"""

import asyncio
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from websockets.exceptions import ConnectionClosed

from backend.config import GIB, MIB, Settings
from backend.control import PowerControl, dispatch_fan_profile, parse_active_scheme
from backend.main import PrivateNetwork, bound_sockets, create_app, trusted_host
from backend.monitor import IDLE_JOB, Monitor, Rules, active_interfaces, merge_hardware, parse_lhm, read_sensor
from backend.scratchpad import FileStream, Scratchpad


async def pending():
    await asyncio.Future()


class HardwareDetailsTests(unittest.TestCase):
    def test_lhm_precedence_cpu_threads_gpu_clocks_memory_and_grouped_fans(self):
        def device(identifier, title, *sensors):
            return {"HardwareId": identifier, "Text": title, "Children": [
                {"SensorId": f"{identifier}/{kind}/{index}", "Text": label, "Value": value}
                for index, (kind, label, value) in enumerate(sensors)
            ]}

        tree = {"Children": [
            device("/intelcpu/0", "Intel CPU",
                   ("load", "CPU Total", "35 %"), ("load", "CPU Core #1 Thread #1", "20 %"),
                   ("load", "CPU Core #1 Thread #2", "30 %"), ("load", "CPU Core #2", "60 %"),
                   ("clock", "P-Core #1", "5200 MHz"), ("clock", "E-Core #1", "4000 MHz"),
                   ("clock", "Bus Speed", "100 MHz"), ("voltage", "CPU Core", "1.120 V"),
                   ("temperature", "CPU Package", "68 °C"), ("temperature", "Core Max", "72 °C"),
                   ("power", "CPU Package", "82 W")),
            device("/gpu-nvidia/0", "GPU",
                   ("clock", "GPU Core", "2400 MHz"), ("clock", "GPU Memory", "9000 MHz"),
                   ("temperature", "GPU Core", "51 °C"), ("temperature", "GPU Hot Spot", "66 °C"),
                   ("fan", "GPU Fan 1", "850 RPM"), ("control", "GPU Fan 1", "29 %"),
                   ("fan", "GPU Fan 2", "900 RPM"), ("control", "GPU Fan 2", "33 %"),
                   ("smalldata", "GPU Memory Used", "5000 MB"), ("smalldata", "GPU Memory Free", "3000 MB")),
            device("/ram", "Total Memory", ("data", "Memory Used", "12 GB"), ("data", "Memory Available", "20 GB"),
                   ("load", "Memory", "37.5 %")),
            device("/vram", "Virtual Memory", ("data", "Memory Used", "20 GB"), ("data", "Memory Available", "30 GB")),
            device("/lpc/chip/0", "Motherboard", ("fan", "Fan #1", "1300 RPM"), ("fan", "Fan #2", "750 RPM"),
                   ("fan", "Fan #3", "0 RPM"), ("fan", "Fan #4", "820 RPM"), ("fan", "Fan #6", "3000 RPM")),
        ]}
        hardware = parse_lhm(tree, {"1": "cpu", "6": "pump"})
        os_sample = {"cpu": {"usage_percent": 90, "physical_cores": 2},
                     "memory": {"percent": 80, "top_processes": [{"name": "fixture"}]},
                     "network": {"total_upload_bytes": 512}}
        self.assertEqual(merge_hardware(os_sample, hardware), {"/intelcpu/0": 68})
        cpu = os_sample["cpu"]
        self.assertEqual([cpu[key] for key in ("usage_percent", "physical_cores", "average_clock_mhz", "max_clock_mhz",
                                              "voltage_v", "max_core_temperature_c", "fan_rpm")],
                         [35, 2, 4600, 5200, 1.12, 72, 1300])
        self.assertEqual([core["usage_percent"] for core in cpu["cores"]], [25, 60])
        self.assertEqual(cpu["pump_rpm"], 3000)
        gpu = os_sample["gpus"][0]
        self.assertEqual([gpu[key] for key in ("core_clock_mhz", "memory_clock_mhz", "hotspot_temperature_c", "vram_total_bytes")],
                         [2400, 9000, 66, 8000 * MIB])
        self.assertEqual([fan["percent"] for fan in gpu["fans"]], [29, 33])
        self.assertEqual([(fan["id"], fan["rpm"]) for fan in os_sample["fan_summary"]],
                         [("cpu", 1300), ("gpu", 900), ("chassis", 820)])
        self.assertTrue(all(fan["rpm"] > 0 for fan in os_sample["fans"]))
        self.assertEqual(os_sample["memory"]["percent"], 37.5)
        self.assertEqual(os_sample["memory"]["total_bytes"], 32 * GIB)
        self.assertEqual(os_sample["memory"]["virtual"]["total_bytes"], 50 * GIB)
        self.assertEqual(os_sample["memory"]["top_processes"][0]["name"], "fixture")
        self.assertEqual(os_sample["network"]["total_upload_bytes"], 512)

    def test_lhm_disconnect_preserves_psutil_values(self):
        sample = {"cpu": {"usage_percent": 90}, "memory": {"percent": 80}, "network": {}}
        self.assertEqual(merge_hardware(sample, None), {})
        self.assertFalse(sample["lhm_connected"])
        self.assertEqual(sample["cpu"]["usage_percent"], 90)
        self.assertEqual(sample["memory"]["percent"], 80)
        self.assertEqual(sample["fan_summary"], [])

    def test_network_addrs_distinguish_active_tailscale_and_local(self):
        from socket import AF_INET, AF_INET6
        up, down = SimpleNamespace(isup=True), SimpleNamespace(isup=False)
        addresses = {
            "Ethernet": [SimpleNamespace(family=AF_INET, address="192.168.1.2"),
                         SimpleNamespace(family=AF_INET6, address="fe80::1%7")],
            "Tailscale": [SimpleNamespace(family=AF_INET, address="100.100.0.2")],
            "Wi-Fi": [SimpleNamespace(family=AF_INET, address="192.168.2.3")],
        }
        with patch("backend.monitor.psutil.net_if_stats", return_value={"Ethernet": up, "Tailscale": up, "Wi-Fi": down}), \
             patch("backend.monitor.psutil.net_if_addrs", return_value=addresses):
            self.assertEqual(active_interfaces(), [
                {"name": "Ethernet", "addresses": ["192.168.1.2"], "tailscale": False},
                {"name": "Tailscale", "addresses": ["100.100.0.2"], "tailscale": True},
            ])


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_worker_survives_unexpected_errors_without_leaking_url(self):
        monitor = Monitor(Settings())
        delivered = asyncio.Event()
        secret = "https://api.day.app/private-key"

        async def send(_client, _config, event):
            errors = {"first": RuntimeError, "second": OSError}
            if event["type"] in errors:
                raise errors[event["type"]](secret)
            delivered.set()

        for kind in ("first", "second", "third"):
            monitor.notifications.put_nowait({"type": kind})
        with self.assertLogs("backend.monitor", level="WARNING") as logs:
            with patch("backend.monitor.send_webhook", side_effect=send):
                worker = asyncio.create_task(monitor.send_notifications(Mock()))
                try:
                    await asyncio.wait_for(delivered.wait(), 1)
                    self.assertFalse(worker.done())
                finally:
                    worker.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await worker
        self.assertNotIn(secret, str(logs.output))
        for kind, error in (("first", "RuntimeError"), ("second", "OSError")):
            self.assertTrue(any(kind in line and error in line for line in logs.output))

    async def test_lhm_failure_backoff_recovers_without_spam(self):
        monitor = Monitor(Settings(), {"platform": "windows", "name": "Fixture"})
        response = Mock()
        response.json.return_value = {"Children": []}
        client = SimpleNamespace(get=AsyncMock(side_effect=[response, response, response, response]))
        with patch("backend.monitor.monotonic") as clock, self.assertLogs("backend.monitor", level="INFO") as logs:
            clock.return_value = 0.0
            with patch("backend.monitor.parse_lhm", side_effect=TypeError("bad response")):
                self.assertIsNone(await monitor.read_lhm(client))
                clock.return_value = 1.5
                self.assertIsNone(await monitor.read_lhm(client))
                self.assertEqual(client.get.await_count, 1)
                clock.return_value = 6.0
                self.assertIsNone(await monitor.read_lhm(client))
            clock.return_value = 12.0
            self.assertIsNotNone(await monitor.read_lhm(client))
            self.assertIsNotNone(await monitor.read_lhm(client))
        self.assertEqual(client.get.await_count, 4)
        self.assertEqual(len(logs.output), 2)


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.rules = Rules(Settings(task={"high_seconds": 3, "low_seconds": 3}, alerts={"temperature_seconds": 3}))

    def tick(self, now, *, cpu=5, gpus=(), disks=()):
        snapshot = {
            "timestamp": str(now), "cpu": {"usage_percent": cpu},
            "gpus": [{"id": key, "usage_percent": load, "temperature_c": temp} for key, load, temp in gpus],
            "disks": [{"mountpoint": volume, "free_bytes": GIB} for volume in disks],
        }
        events = self.rules.evaluate(snapshot, {}, now)
        return snapshot["task"], events

    def test_short_gpu_outage_preserves_running_job(self):
        gpu = ("gpu0", 95, 40)
        self.tick(0, gpus=[gpu])
        self.tick(3, gpus=[gpu])
        self.assertEqual(self.tick(4.5)[0]["state"], "running")
        self.assertEqual(self.tick(9, gpus=[gpu])[0]["state"], "running")
        self.tick(10.5, gpus=[("gpu0", 0, 40)])
        job, events = self.tick(13.5, gpus=[("gpu0", 0, 40)])
        self.assertEqual(job["state"], "completed")
        self.assertEqual([event["type"] for event in events], ["task_completed"])

    def test_long_gpu_outage_unblocks_cpu_without_fake_completion(self):
        gpu = ("gpu0", 95, 40)
        self.tick(0, gpus=[gpu])
        self.tick(3, gpus=[gpu])
        for now in range(6, 66, 3):
            job, events = self.tick(now)
            self.assertEqual(events, [])
            self.assertEqual(job["state"], "running")
        self.assertEqual(self.tick(66), (IDLE_JOB, []))
        self.tick(67.5, cpu=95)
        self.assertEqual(self.tick(70.5, cpu=95)[0]["source"], "cpu")

    def test_sample_gaps_reset_only_after_ten_seconds(self):
        self.tick(0, cpu=95)
        self.assertEqual(self.tick(5, cpu=95)[0]["state"], "running")
        self.tick(6)
        self.assertIsNone(self.rules.missing_since)
        gpu = ("gpu0", 95, 40)
        self.rules = Rules(Settings(task={"high_seconds": 3, "low_seconds": 3}))
        self.tick(0, gpus=[gpu])
        self.tick(3, gpus=[gpu])
        self.tick(6)
        self.assertEqual(self.rules.missing_since, 6)
        self.assertEqual(self.tick(20)[0]["state"], "running")
        self.assertEqual(self.rules.missing_since, 20)

    def test_alarm_cooldown_is_independent_per_disk_and_gpu(self):
        _, events = self.tick(0, disks=["D:/"], gpus=[("gpu0", 5, 96)])
        self.assertIn("D:/", events[0]["message"])
        _, events = self.tick(3, disks=["D:/", "C:/"], gpus=[("gpu0", 5, 96), ("gpu1", 5, 97)])
        self.assertEqual([event["type"] for event in events], ["gpu_temperature", "disk_space"])
        self.assertIn("gpu0", events[0]["message"])
        self.assertIn("C:/", events[1]["message"])
        self.assertNotIn("D:/", events[1]["message"])
        _, events = self.tick(6, disks=["D:/", "C:/"], gpus=[("gpu0", 5, 96), ("gpu1", 5, 97)])
        self.assertEqual(len(events), 1)
        self.assertIn("gpu1", events[0]["message"])
        self.assertNotIn("gpu0", events[0]["message"])
        self.assertEqual(set(events[0]), {"type", "title", "message", "timestamp"})

    def test_lhm_uses_full_localized_number_and_recognizes_intel_gpu(self):
        for kind, value, expected in (
            ("Data", "1,536.0 MB", 1536 * MIB), ("Data", "1.536,0 MB", 1536 * MIB),
            ("Fan", "1,250 RPM", 1250), ("Temperature", "65,5 °C", 65.5), ("Data", "8.2 GB", 8.2 * GIB),
        ):
            with self.subTest(value=value):
                self.assertAlmostEqual(read_sensor({"Type": kind, "Value": value}, "/gpu/0/data/0").value, expected)
        tree = {"Children": [{
            "HardwareId": "/gpu-intel-discrete/0", "Text": "Intel Arc",
            "Children": [{"SensorId": "/gpu-intel-discrete/0/load/0", "Text": "GPU Core", "Value": "33 %"}],
        }]}
        self.assertEqual(parse_lhm(tree)["gpus"][0]["usage_percent"], 33)


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_and_websocket_default_port_and_cross_origin(self):
        for host in ("127.0.0.1:abc", "127.0.0.1:65536", "[::1]junk", "localhost:"):
            self.assertFalse(trusted_host(host))
        app = AsyncMock()
        guard = PrivateNetwork(app)
        for protocol in ("http", "websocket"):
            for origin, accepted in (("http://100.64.0.1", True), ("https://100.64.0.1", False),
                                     ("http://100.64.0.1:81", False), ("http://foreign.test", False)):
                app.reset_mock()
                send = AsyncMock()
                scope = {"type": protocol, "method": "POST", "client": ("100.64.0.2", 1234),
                         "headers": [(b"host", b"100.64.0.1:80"), (b"origin", origin.encode())]}
                await guard(scope, AsyncMock(), send)
                self.assertEqual(app.await_count, int(accepted))
                if not accepted:
                    key, status = ("status", 403) if protocol == "http" else ("code", 1008)
                    self.assertEqual(send.await_args_list[0].args[0][key], status)

    async def test_tailscale_serve_https_origin_and_websocket(self):
        host = "workstation.example-tail.ts.net"
        https_origin = f"https://{host}"
        cases = (
            (https_origin, "same-origin", "127.0.0.1", True),
            (f"{https_origin}:443", "same-origin", "127.0.0.1", True),
            (f"{https_origin}:3993", "same-site", "127.0.0.1", False),
            ("https://other.example-tail.ts.net", "same-site", "127.0.0.1", False),
            ("https://unrelated.example", None, "127.0.0.1", False),
            (https_origin, "cross-site", "127.0.0.1", False),
            (https_origin, "same-origin", "192.168.1.5", False),
        )
        for protocol in ("http", "websocket"):
            for origin, fetch_site, client_ip, allowed in cases:
                with self.subTest(protocol=protocol, origin=origin, fetch_site=fetch_site, client_ip=client_ip):
                    app, send = AsyncMock(), AsyncMock()
                    headers = [(b"host", host.encode()), (b"origin", origin.encode()), (b"x-forwarded-proto", b"https")]
                    if fetch_site is not None:
                        headers.append((b"sec-fetch-site", fetch_site.encode()))
                    scope = {"type": protocol, "client": (client_ip, 1234), "headers": headers}
                    if protocol == "http":
                        scope.update(method="POST", scheme="http")
                    else:
                        scope.update(scheme="ws")
                    await PrivateNetwork(app)(scope, AsyncMock(), send)
                    if allowed:
                        app.assert_awaited_once()
                        send.assert_not_awaited()
                    else:
                        app.assert_not_awaited()
                        response = send.await_args_list[0].args[0]
                        if protocol == "http":
                            self.assertEqual(response["status"], 403)
                        else:
                            self.assertEqual(response, {"type": "websocket.close", "code": 1008})

    @unittest.skipUnless(sys.platform == "win32", "Windows SO_EXCLUSIVEADDRUSE")
    def test_wildcard_listener_excludes_another_instance(self):
        with bound_sockets("0.0.0.0", 0) as listeners:
            port = listeners[0].getsockname()[1]
            for host in ("0.0.0.0", "127.0.0.1"):
                with self.subTest(host=host), self.assertRaises(OSError):
                    with bound_sockets(host, port):
                        pass
        with bound_sockets("0.0.0.0", port):
            pass

    def websocket(self):
        app = create_app(Settings())
        monitor = Monitor(Settings())
        monitor.latest = {"test": True}
        app.state.monitor = monitor
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/ws/metrics")
        return endpoint, monitor, SimpleNamespace(app=app, accept=AsyncMock(), close=AsyncMock())

    async def test_websocket_blocked_close_is_bounded(self):
        endpoint, monitor, ws = self.websocket()
        ws.receive = pending
        close_started = None

        async def stalled_send(_snapshot):
            await pending()

        async def stalled_close(**_):
            nonlocal close_started
            close_started = monotonic()
            await pending()

        ws.send_json = AsyncMock(side_effect=stalled_send)
        ws.close.side_effect = stalled_close
        await asyncio.wait_for(endpoint(ws), 8.5)
        ws.close.assert_awaited_once_with(code=1013)
        self.assertLess(monotonic() - close_started, 2.5)
        self.assertFalse(monitor.subscribers)

    async def test_websocket_reads_both_disconnect_errors_and_removes_subscriber(self):
        endpoint, monitor, ws = self.websocket()
        read_started = asyncio.Event()

        async def receive():
            read_started.set()
            raise RuntimeError("remote disconnect")

        async def send(_snapshot):
            await read_started.wait()
            raise ConnectionClosed(None, None)

        ws.receive = receive
        ws.send_json = send
        await endpoint(ws)
        self.assertFalse(monitor.subscribers)
        ws.close.assert_not_called()


class ScratchpadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_headers_and_names_use_actual_file_size(self):
        with TemporaryDirectory() as folder:
            data = Path(folder)
            store = Scratchpad(data)
            store.initialize()
            app = create_app(Settings(), data)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost") as client:
                for name in ("   ", ".", ".."):
                    result = await client.post("/api/scratchpad/file", files={"file": (name, b"x")})
                    self.assertEqual(result.json()["filename"], "upload")
                result = await client.post("/api/scratchpad/file", files={"file": ("测试报告.pdf", b"abc")})
                item = result.json()["id"]
                with store.connect() as db:
                    db.execute("UPDATE scratchpad SET size_bytes = 2 WHERE id = ?", (item,))
                response = await client.get(f"/api/scratchpad/files/{item}")
                self.assertEqual(response.headers["content-length"], "3")
                self.assertEqual(response.content, b"abc")
                self.assertIn('filename="download.pdf"', response.headers["content-disposition"])
                self.assertIn("filename*=UTF-8''%E6%B5%8B%E8%AF%95%E6%8A%A5%E5%91%8A.pdf", response.headers["content-disposition"])
                self.assertEqual((await client.delete(f"/api/scratchpad/{item}")).status_code, 204)
                self.assertEqual((await client.get(f"/api/scratchpad/files/{item}")).status_code, 404)

    async def test_cancel_during_threaded_read_releases_file_before_delete(self):
        with TemporaryDirectory() as folder:
            store = Scratchpad(Path(folder))
            store.initialize()
            item = store.add_file(BytesIO(b"data"), "example.txt", 4, "text/plain")["id"]
            reading, release = Event(), Event()
            row, disk_file = store.open_file(item)

            def delayed_read(size):
                reading.set()
                if not release.wait(2):
                    raise TimeoutError("test did not release the file reader")
                return disk_file.read(size)

            stream_file = SimpleNamespace(fileno=disk_file.fileno, close=disk_file.close, read=delayed_read)
            with patch.object(store, "open_file", return_value=(row, stream_file)):
                task = asyncio.create_task(FileStream(store, item, False)(
                    {"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), AsyncMock(),
                ))
                try:
                    self.assertTrue(await asyncio.to_thread(reading.wait, 1))
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(disk_file.closed)
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            self.assertTrue(disk_file.closed)
            store.delete(item)

    async def test_header_failure_does_not_leak_file(self):
        with TemporaryDirectory() as folder:
            store = Scratchpad(Path(folder))
            store.initialize()
            item = store.add_file(BytesIO(b"data"), "example.txt", 4, "text/plain")["id"]
            with patch("backend.scratchpad.content_disposition", side_effect=RuntimeError("headers failed")):
                with self.assertRaisesRegex(RuntimeError, "headers failed"):
                    await FileStream(store, item, False)({"type": "http"}, AsyncMock(), AsyncMock())
            store.delete(item)


class ControlTests(unittest.TestCase):
    def test_file_sensor_tracks_vitals_and_external_plan_changes(self):
        with TemporaryDirectory() as folder:
            target = Path(folder) / "fancontrol-mode.sensor"
            settings = Settings(fancontrol_mode_sensor=True)
            controller = PowerControl(settings, sensor_file=target)
            silent = str(settings.power_modes["silent"].guid)
            performance = str(settings.power_modes["performance"].guid)
            with patch("backend.control.powercfg", return_value=f"Power Scheme GUID: {silent}") as power:
                self.assertEqual(controller.active_power()["mode"], "silent")
                self.assertEqual(target.read_text(encoding="ascii"), "30\n")
                self.assertEqual(controller.set_power("balanced")["fan_control"]["status"], "sensor_updated")
                self.assertEqual(target.read_text(encoding="ascii"), "45\n")
                power.return_value = f"Power Scheme GUID: {performance}"
                self.assertEqual(controller.active_power()["mode"], "performance")
                self.assertEqual(target.read_text(encoding="ascii"), "0\n")

    def test_read_plan_names_with_both_styles_of_parentheses(self):
        guid = "381b4222-f694-41f0-9685-ff5bb260df2e"
        self.assertEqual(parse_active_scheme(f"GUID: {guid} (Balanced (custom))")[1], "Balanced (custom)")
        self.assertEqual(parse_active_scheme(f"电源计划 GUID: {guid} （平衡）")[1], "平衡")

    def test_fan_process_uses_detached_standard_handles_and_is_reaped(self):
        with TemporaryDirectory() as folder:
            exe, profile = Path(folder) / "FanControl.exe", Path(folder) / "profile.json"
            exe.touch()
            profile.write_text("{}", encoding="utf-8")
            with patch("backend.control.subprocess.Popen") as popen, patch("backend.control.Thread") as thread:
                process = popen.return_value
                process.poll.return_value = None
                self.assertEqual(dispatch_fan_profile(exe, profile)["status"], "dispatched")
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
                popen.assert_called_once_with(
                    [str(exe.resolve()), "-c", str(profile.resolve())], cwd=exe.parent.resolve(), creationflags=flags,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                )
                self.assertEqual(thread.call_args.kwargs["target"], process.wait)
                thread.return_value.start.assert_called_once()
