# Moonraker component for autonomous bed surface temperature calibration.
#
# Maps heater_bed sensor readings to actual glass surface temperature
# (bed_glass sensor), optionally accounting for chamber temperature.
# Runs entirely on the printer — survives app restarts, auth loss, network drops.
#
# Config example (moonraker.conf):
#   [bed_surface_calibration]
#   stabilize_time: 600
#   samples_per_point: 10
#   bed_heater: heater_bed
#   chamber_heater: Active_Chamber
#   surface_sensor: bed_glass
#   tolerance: 2.0
#
# Copyright (C) 2026 Protype — GPLv3

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Safety limits
BED_MAX_TEMP = 250
CHAMBER_MAX_TEMP = 160
BED_HEAT_TIMEOUT = 900       # 15 min
BED_COOL_TIMEOUT = 2400      # 40 min
CHAMBER_HEAT_TIMEOUT = 2400  # 40 min
CHAMBER_COOL_TIMEOUT = 3600  # 60 min

# Early-exit stabilization thresholds
EARLY_STABLE_MIN_TIME = 30
EARLY_STABLE_WINDOW = 20
EARLY_STABLE_MAX_DELTA = 5.0
EARLY_STABLE_MAX_DRIFT = 1.5


class BedSurfaceCalibration:
    def __init__(self, config) -> None:
        self.server = config.get_server()
        self.name = config.get_name()

        self.stabilize_time: int = config.getint("stabilize_time", 600)
        self.samples_per_point: int = config.getint("samples_per_point", 10)
        self.bed_heater: str = config.get("bed_heater", "heater_bed")
        self.chamber_heater: str = config.get("chamber_heater", "Active_Chamber")
        self.surface_sensor: str = config.get("surface_sensor", "bed_glass")
        self.tolerance: float = config.getfloat("tolerance", 2.0)
        self.results_dir: str = os.path.expanduser(
            config.get("results_dir", "~/calibration_results")
        )
        os.makedirs(self.results_dir, exist_ok=True)

        # Runtime state
        self._state: str = "idle"
        self._sub_state: Optional[str] = None
        self._targets: List[Dict] = []
        self._points: List[Dict] = []
        self._current_index: int = 0
        self._config: Dict = {}
        self._error: Optional[str] = None
        self._started_at: Optional[float] = None
        self._run_id: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._abort: bool = False
        self._skip_event: asyncio.Event = asyncio.Event()
        self._stabilize_counter: int = 0

        # Register API endpoints
        self.server.register_endpoint(
            "/server/calibration/start", ["POST"],
            self._handle_start
        )
        self.server.register_endpoint(
            "/server/calibration/abort", ["POST"],
            self._handle_abort
        )
        self.server.register_endpoint(
            "/server/calibration/skip", ["POST"],
            self._handle_skip
        )
        self.server.register_endpoint(
            "/server/calibration/status", ["GET"],
            self._handle_status
        )
        self.server.register_endpoint(
            "/server/calibration/results", ["GET"],
            self._handle_results
        )
        self.server.register_endpoint(
            "/server/calibration/history", ["GET"],
            self._handle_history
        )
        self.server.register_endpoint(
            "/server/calibration/history/delete", ["POST"],
            self._handle_delete_history
        )

        self.server.register_event_handler(
            "server:klippy_disconnect", self._on_klippy_disconnect
        )

        self.server.register_notification("calibration:point_collected")
        self.server.register_notification("calibration:state_changed")
        self.server.register_notification("calibration:completed")

    # ─── Klipper helpers ─────────────────────────────────

    async def _query_temps(self) -> Dict[str, float]:
        klippy = self.server.lookup_component("klippy_apis")
        result = await klippy.query_objects({
            self.bed_heater: None,
            f"heater_generic {self.chamber_heater}": None,
            f"temperature_sensor {self.surface_sensor}": None,
        })
        bed = result.get(self.bed_heater, {})
        chamber = result.get(f"heater_generic {self.chamber_heater}", {})
        glass = result.get(f"temperature_sensor {self.surface_sensor}", {})
        return {
            "bed": float(bed.get("temperature", 0)),
            "bed_target": float(bed.get("target", 0)),
            "bed_power": float(bed.get("power", 0)),
            "chamber": float(chamber.get("temperature", 0)),
            "chamber_target": float(chamber.get("target", 0)),
            "glass": float(glass.get("temperature", 0)),
        }

    async def _send_gcode(self, script: str) -> None:
        klippy = self.server.lookup_component("klippy_apis")
        await klippy.run_gcode(script)

    async def _set_bed_temp(self, target: int) -> None:
        await self._send_gcode(
            f"SET_HEATER_TEMPERATURE HEATER={self.bed_heater} TARGET={target}"
        )

    async def _set_chamber_temp(self, target: int) -> None:
        await self._send_gcode(
            f"SET_HEATER_TEMPERATURE HEATER={self.chamber_heater} TARGET={target}"
        )

    async def _cooldown(self) -> None:
        try:
            await self._set_bed_temp(0)
            await self._set_chamber_temp(0)
        except Exception as e:
            log.warning(f"Cooldown error (non-fatal): {e}")

    def _set_sub_state(self, sub: str) -> None:
        self._sub_state = sub
        self.server.send_event("calibration:state_changed", {
            "state": self._state,
            "sub_state": sub,
            "index": self._current_index,
        })

    # ─── Target generation ───────────────────────────────

    @staticmethod
    def _build_targets(cfg: Dict, excluded: List[int]) -> List[Dict]:
        """Build targets sorted to minimise cooling waits.

        Primary sort: bed ascending (bed never cools).
        Secondary sort: chamber in snake/boustrophedon order
        (even bed-groups ascending, odd descending) so chamber
        swings by at most one step between groups.
        """
        targets: List[Dict] = []
        bed_start = cfg.get("bed_start", 50)
        bed_end = min(cfg.get("bed_end", 180), BED_MAX_TEMP)
        bed_step = max(cfg.get("bed_step", 20), 1)
        chamber_start = cfg.get("chamber_start", 35)
        chamber_end = min(cfg.get("chamber_end", 75), CHAMBER_MAX_TEMP)
        chamber_step = max(cfg.get("chamber_step", 20), 1)
        phase = cfg.get("phase", "both")

        bed_points = list(range(bed_start, bed_end + 1, bed_step))
        if not bed_points:
            return targets

        # Phase 1: chamber off, bed ascending
        if phase in ("both", "1"):
            for bed in bed_points:
                targets.append({
                    "phase": 1, "bed_target": bed, "chamber_target": 0
                })

        # Phase 2: bed ascending (primary), chamber snake (secondary)
        if phase in ("both", "2"):
            ch_points = [
                t for t in range(chamber_start, chamber_end + 1, chamber_step)
                if t > 0
            ]
            bed_group_idx = 0
            for bed in bed_points:
                valid = [c for c in ch_points if bed >= c]
                if not valid:
                    continue
                ordered = valid if bed_group_idx % 2 == 0 else list(reversed(valid))
                for ch in ordered:
                    targets.append({
                        "phase": 2, "bed_target": bed, "chamber_target": ch
                    })
                bed_group_idx += 1

        excluded_set = set(excluded)
        return [t for i, t in enumerate(targets) if i not in excluded_set]

    # ─── Calibration loop ────────────────────────────────

    async def _run_calibration(self) -> None:
        try:
            prev_chamber = -1

            for idx in range(len(self._targets)):
                if self._abort:
                    break

                self._current_index = idx
                target = self._targets[idx]
                bed_target = target["bed_target"]
                chamber_target = target["chamber_target"]

                if chamber_target != prev_chamber:
                    if chamber_target > 0:
                        if prev_chamber > chamber_target:
                            self._set_sub_state("cooling_chamber")
                            await self._set_chamber_temp(chamber_target)
                            reached = await self._wait_for_temp(
                                "chamber", chamber_target,
                                CHAMBER_COOL_TIMEOUT, cooling=True,
                            )
                        else:
                            self._set_sub_state("heating_chamber")
                            await self._set_chamber_temp(chamber_target)
                            reached = await self._wait_for_temp(
                                "chamber", chamber_target,
                                CHAMBER_HEAT_TIMEOUT,
                            )
                        if not reached:
                            log.info(
                                f"Chamber timeout at point {idx} — "
                                f"sampling at current temperature"
                            )
                        await self._set_bed_temp(bed_target)
                        if self._abort:
                            break
                    elif prev_chamber > 0:
                        await self._set_chamber_temp(0)
                    prev_chamber = chamber_target

                temps = await self._query_temps()
                if temps["bed"] > bed_target + self.tolerance:
                    self._set_sub_state("cooling_bed")
                    await self._set_bed_temp(bed_target)
                    reached = await self._wait_for_temp(
                        "bed", bed_target, BED_COOL_TIMEOUT, cooling=True,
                    )
                else:
                    self._set_sub_state("heating_bed")
                    await self._set_bed_temp(bed_target)
                    reached = await self._wait_for_temp(
                        "bed", bed_target, BED_HEAT_TIMEOUT,
                    )

                if not reached:
                    log.info(
                        f"Bed timeout at point {idx} — "
                        f"sampling at current temperature"
                    )

                if self._abort:
                    break

                self._set_sub_state("stabilizing")
                self._stabilize_counter = 0
                await self._stabilize(bed_target)

                if self._abort:
                    break

                self._set_sub_state("sampling")
                point = await self._sample(target)
                self._points.append(point)

                self.server.send_event("calibration:point_collected", {
                    "index": idx,
                    "total": len(self._targets),
                    "point": point,
                })

                self._save_progress()

                if self._abort:
                    break

            await self._cooldown()
            if self._abort:
                self._state = "idle" if not self._points else "done"
            else:
                self._state = "done"
            self._sub_state = None
            self._save_results()

            self.server.send_event("calibration:completed", {
                "state": self._state,
                "points": len(self._points),
                "run_id": self._run_id,
            })

        except Exception as e:
            log.exception("Calibration error")
            self._state = "error"
            self._error = str(e)
            self._sub_state = None
            await self._cooldown()
            self._save_results()

    async def _wait_for_temp(
        self,
        sensor: str,
        target: float,
        timeout: int,
        cooling: bool = False,
    ) -> bool:
        """Wait for sensor to reach target. Returns True if reached, False on timeout."""
        start = time.monotonic()
        while not self._abort:
            if self._skip_event.is_set():
                self._skip_event.clear()
                return True

            temps = await self._query_temps()
            current = temps[sensor]

            if cooling:
                if current <= target + self.tolerance:
                    return True
            else:
                if abs(current - target) <= self.tolerance:
                    return True

            elapsed = time.monotonic() - start
            if elapsed > timeout:
                log.warning(
                    f"Timeout: {sensor} did not reach {target}\u00b0C "
                    f"in {timeout}s (current: {current:.1f}\u00b0C) — skipping point"
                )
                return False

            await asyncio.sleep(1)

        return False

    async def _stabilize(self, bed_target: float) -> None:
        deltas: List[float] = []
        counter = 0

        while counter < self.stabilize_time and not self._abort:
            if self._skip_event.is_set():
                self._skip_event.clear()
                return

            temps = await self._query_temps()
            bed = temps["bed"]
            glass = temps["glass"]
            delta = abs(bed - glass)
            deltas.append(delta)
            counter += 1
            self._stabilize_counter = counter

            drift = abs(bed - bed_target)
            if drift > self.tolerance * 10:
                counter = 0
                deltas.clear()
                self._stabilize_counter = 0
            elif drift > self.tolerance * 5:
                counter = max(0, counter - 2)
                self._stabilize_counter = counter

            if counter >= EARLY_STABLE_MIN_TIME and len(deltas) >= EARLY_STABLE_WINDOW:
                window = deltas[-EARLY_STABLE_WINDOW:]
                max_d = max(window)
                min_d = min(window)
                avg_d = sum(window) / len(window)
                if avg_d < EARLY_STABLE_MAX_DELTA and (max_d - min_d) < EARLY_STABLE_MAX_DRIFT:
                    return

            await asyncio.sleep(1)

    async def _sample(self, target: Dict) -> Dict:
        beds: List[float] = []
        glasses: List[float] = []
        chambers: List[float] = []

        for _ in range(self.samples_per_point):
            if self._abort:
                break
            if self._skip_event.is_set():
                self._skip_event.clear()
                break
            temps = await self._query_temps()
            beds.append(temps["bed"])
            glasses.append(temps["glass"])
            chambers.append(temps["chamber"])
            await asyncio.sleep(1)

        if not beds:
            beds = [0.0]
            glasses = [0.0]
            chambers = [0.0]

        bed_avg = sum(beds) / len(beds)
        glass_avg = sum(glasses) / len(glasses)
        chamber_avg = sum(chambers) / len(chambers)

        return {
            "phase": target["phase"],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bedTarget": target["bed_target"],
            "bedSensorAvg": round(bed_avg, 1),
            "bedSensorMin": round(min(beds), 1),
            "bedSensorMax": round(max(beds), 1),
            "chamberTarget": target["chamber_target"],
            "chamberTemp": round(chamber_avg, 1),
            "surfaceTemp": round(glass_avg, 1),
            "delta": round(bed_avg - glass_avg, 1),
        }

    # ─── Persistence ─────────────────────────────────────

    def _save_progress(self) -> None:
        path = os.path.join(
            self.results_dir, f"_running_{self._run_id}.json"
        )
        try:
            data = {
                "id": self._run_id,
                "state": "running",
                "config": self._config,
                "targets": self._targets,
                "points": self._points,
                "current_index": self._current_index,
                "started_at": self._started_at,
            }
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            log.warning(f"Failed to save progress: {e}")

    def _save_results(self) -> None:
        running_path = os.path.join(
            self.results_dir, f"_running_{self._run_id}.json"
        )
        if os.path.exists(running_path):
            try:
                os.remove(running_path)
            except OSError:
                pass

        if not self._points:
            return

        data = {
            "id": self._run_id,
            "completedAt": datetime.now().isoformat(),
            "state": self._state,
            "config": self._config,
            "targets": self._targets,
            "points": self._points,
            "pointCount": len(self._points),
        }
        path = os.path.join(
            self.results_dir, f"calibration_{self._run_id}.json"
        )
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            log.info(
                f"Calibration results saved: {path} "
                f"({len(self._points)} points)"
            )
        except Exception as e:
            log.error(f"Failed to save results: {e}")

    def _try_restore_running(self) -> None:
        """On startup, check for a crashed running session."""
        try:
            for fname in os.listdir(self.results_dir):
                if fname.startswith("_running_") and fname.endswith(".json"):
                    path = os.path.join(self.results_dir, fname)
                    with open(path) as f:
                        data = json.load(f)
                    data["state"] = "done"
                    data["completedAt"] = datetime.now().isoformat()
                    data["pointCount"] = len(data.get("points", []))
                    run_id = data.get("id", fname.replace("_running_", "").replace(".json", ""))
                    result_path = os.path.join(
                        self.results_dir, f"calibration_{run_id}.json"
                    )
                    with open(result_path, "w") as f:
                        json.dump(data, f, indent=2)
                    os.remove(path)
                    log.info(f"Recovered crashed session: {run_id}")
        except Exception as e:
            log.warning(f"Session recovery error: {e}")

    # ─── API handlers ────────────────────────────────────

    async def _handle_start(self, web_request) -> Dict[str, Any]:
        if self._state == "running":
            raise self.server.error("Calibration already running", 409)

        config = web_request.get("config")
        if not config:
            raise self.server.error("Missing 'config' parameter")

        excluded = web_request.get("excluded", [])

        self._config = config
        self._targets = self._build_targets(config, excluded)
        if not self._targets:
            raise self.server.error("No calibration targets after exclusion")

        self._points = []
        self._current_index = 0
        self._state = "running"
        self._sub_state = None
        self._error = None
        self._abort = False
        self._skip_event.clear()
        self._run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._started_at = time.time()

        self._save_progress()

        self._task = asyncio.ensure_future(self._run_calibration())

        return {
            "status": "started",
            "run_id": self._run_id,
            "total_points": len(self._targets),
        }

    async def _handle_abort(self, web_request) -> Dict[str, Any]:
        if self._state != "running":
            return {"status": "not_running"}

        self._abort = True
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()

        return {
            "status": "aborted",
            "points_collected": len(self._points),
        }

    async def _handle_skip(self, web_request) -> Dict[str, Any]:
        if self._state != "running":
            return {"status": "not_running"}
        self._skip_event.set()
        return {"status": "skipping"}

    async def _handle_status(self, web_request) -> Dict[str, Any]:
        temps: Dict[str, float] = {}
        if self._state == "running":
            try:
                temps = await self._query_temps()
            except Exception:
                pass

        elapsed = 0
        if self._started_at:
            elapsed = int(time.time() - self._started_at)

        remaining = 0
        if self._state == "running" and self._targets:
            remaining_pts = len(self._targets) - self._current_index
            avg_per_point = self.stabilize_time + 120 + 12
            remaining = remaining_pts * avg_per_point

        return {
            "state": self._state,
            "runSubState": self._sub_state,
            "currentIndex": self._current_index,
            "totalPoints": len(self._targets),
            "pointsCollected": len(self._points),
            "targets": self._targets,
            "points": self._points,
            "temperatures": temps,
            "config": self._config,
            "runId": self._run_id,
            "error": self._error,
            "elapsed": elapsed,
            "estimatedTimeRemaining": remaining,
            "stabilizeRemaining": (
                max(0, self.stabilize_time - self._stabilize_counter)
                if self._sub_state == "stabilizing" else 0
            ),
            "samplesCollected": 0,
        }

    async def _handle_results(self, web_request) -> Dict[str, Any]:
        run_id = web_request.get_str("run_id", self._run_id or "")
        if not run_id:
            raise self.server.error("Missing run_id")
        path = os.path.join(self.results_dir, f"calibration_{run_id}.json")
        if not os.path.exists(path):
            raise self.server.error(f"Results not found: {run_id}", 404)
        with open(path) as f:
            return json.load(f)

    async def _handle_history(self, web_request) -> Dict[str, Any]:
        entries: List[Dict] = []
        try:
            files = sorted(
                [
                    f for f in os.listdir(self.results_dir)
                    if f.startswith("calibration_") and f.endswith(".json")
                ],
                reverse=True,
            )
            for fname in files[:50]:
                path = os.path.join(self.results_dir, fname)
                try:
                    with open(path) as f:
                        data = json.load(f)
                    entries.append({
                        "id": data.get("id"),
                        "completedAt": data.get("completedAt"),
                        "pointCount": data.get("pointCount", 0),
                        "state": data.get("state"),
                        "config": data.get("config"),
                    })
                except (json.JSONDecodeError, IOError):
                    continue
        except OSError:
            pass
        return {"entries": entries}

    async def _handle_delete_history(self, web_request) -> Dict[str, Any]:
        run_id = web_request.get_str("run_id")
        if not run_id:
            raise self.server.error("Missing run_id")
        path = os.path.join(self.results_dir, f"calibration_{run_id}.json")
        if os.path.exists(path):
            os.remove(path)
            return {"status": "deleted"}
        raise self.server.error(f"Entry not found: {run_id}", 404)

    # ─── Events ──────────────────────────────────────────

    def _on_klippy_disconnect(self) -> None:
        if self._state == "running":
            log.warning("Klipper disconnected during calibration - aborting")
            self._abort = True
            self._error = "Klipper disconnected"

    # ─── Component lifecycle ─────────────────────────────

    async def component_init(self) -> None:
        self._try_restore_running()


def load_component(config):
    return BedSurfaceCalibration(config)
