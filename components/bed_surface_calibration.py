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
        """Build targets from config. Dispatches to v1 or v2 based on algorithm field."""
        algorithm = cfg.get("algorithm", "v1")

        if algorithm == "v2":
            return BedSurfaceCalibration._build_targets_v2(cfg, excluded)
        return BedSurfaceCalibration._build_targets_v1(cfg, excluded)

    @staticmethod
    def _build_targets_v1(cfg: Dict, excluded: List[int]) -> List[Dict]:
        """v1: bed ascending, chamber snake/boustrophedon."""
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

        if phase in ("both", "1"):
            for bed in bed_points:
                targets.append({
                    "phase": 1, "bed_target": bed, "chamber_target": 0
                })

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

    @staticmethod
    def _build_targets_v2(cfg: Dict, excluded: List[int]) -> List[Dict]:
        """v2: explicit level-based plan.

        Rules:
          - Chamber only goes UP between levels (0 → 50 → 75 → 90).
          - Bed within a level only goes UP.
          - Bed cools down between levels when chamber changes.

        Accepts either pre-built 'targets' list or 'levels' definition.
        """
        # If client sent an explicit ordered target list, use it directly
        explicit = cfg.get("targets")
        if explicit and isinstance(explicit, list) and len(explicit) > 0:
            targets = []
            for t in explicit:
                targets.append({
                    "phase": int(t.get("phase", 2 if t.get("chamber_target", 0) > 0 else 1)),
                    "bed_target": int(t["bed_target"]),
                    "chamber_target": int(t.get("chamber_target", 0)),
                    "level": t.get("level"),
                })
            excluded_set = set(excluded)
            return [t for i, t in enumerate(targets) if i not in excluded_set]

        # Build from levels definition
        levels = cfg.get("levels", [])
        if not levels:
            log.warning("v2 config has no targets and no levels — falling back to v1")
            return BedSurfaceCalibration._build_targets_v1(cfg, excluded)

        targets: List[Dict] = []
        for lvl in levels:
            level_num = lvl.get("level", 1)
            chamber = int(lvl.get("chamber_target", 0))
            phase = 1 if chamber == 0 else 2
            bed_pts = sorted(lvl.get("bed_points", []))
            for bed in bed_pts:
                targets.append({
                    "phase": phase,
                    "bed_target": int(min(bed, BED_MAX_TEMP)),
                    "chamber_target": int(min(chamber, CHAMBER_MAX_TEMP)),
                    "level": level_num,
                })

        excluded_set = set(excluded)
        return [t for i, t in enumerate(targets) if i not in excluded_set]

    # ─── Calibration loop ────────────────────────────────

    async def _run_calibration(self) -> None:
        is_v2 = self._config.get("algorithm") == "v2"
        if is_v2:
            await self._run_calibration_v2()
        else:
            await self._run_calibration_v1()

    async def _run_calibration_v1(self) -> None:
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

    async def _run_calibration_v2(self) -> None:
        """v2 calibration loop.

        Algorithm:
          FOR EACH level (chamber only UP):
            1. Heat chamber to target (if level > 1)
            2. Turn off bed (M140 S0)
            3. Wait for bed to cool to first bed_point + tolerance
            4. Chamber stabilization hold
            FOR EACH bed_point (only UP within level):
              5. Heat bed to target
              6. Smart stabilization (sliding window on bed_glass)
              7. Sample (10 readings, record min/max/avg/notes)
              8. Write CSV point
          FINISH: cooldown, compute models
        """
        cfg = self._config
        chamber_stab_time = int(cfg.get("chamber_stabilize_time", 1200))
        smart_min = int(cfg.get("smart_stabilize_min", 300))
        smart_max = int(cfg.get("smart_stabilize_max", 1800))
        smart_window = int(cfg.get("smart_stabilize_window", 120))
        smart_threshold = float(cfg.get("smart_stabilize_threshold", 0.5))
        sample_count = int(cfg.get("sample_count", 10))
        sample_interval = float(cfg.get("sample_interval", 1))
        tolerance = float(cfg.get("tolerance", self.tolerance))

        try:
            prev_chamber = -1
            prev_level = 0

            for idx in range(len(self._targets)):
                if self._abort:
                    break

                self._current_index = idx
                target = self._targets[idx]
                bed_target = target["bed_target"]
                chamber_target = target["chamber_target"]
                level = target.get("level", 1)

                # ── Level transition (chamber change) ──
                if chamber_target != prev_chamber and level != prev_level:
                    if chamber_target > 0:
                        # Heat chamber (only up in v2)
                        self._set_sub_state("heating_chamber")
                        await self._set_chamber_temp(chamber_target)
                        reached = await self._wait_for_temp(
                            "chamber", chamber_target,
                            CHAMBER_HEAT_TIMEOUT,
                        )
                        if not reached:
                            log.info(
                                f"Chamber timeout at level {level} — "
                                f"continuing at current temperature"
                            )
                        if self._abort:
                            break

                        # Cool bed to first point of this level
                        self._set_sub_state("cooling_bed")
                        await self._set_bed_temp(0)
                        first_bed = bed_target
                        cool_reached = await self._wait_for_temp(
                            "bed", first_bed + tolerance,
                            BED_COOL_TIMEOUT, cooling=True,
                        )
                        if not cool_reached:
                            temps = await self._query_temps()
                            log.warning(
                                f"Bed won't cool to {first_bed}°C "
                                f"(stuck at {temps['bed']:.1f}°C, "
                                f"chamber {temps['chamber']:.1f}°C) — "
                                f"chamber may be preventing cooling"
                            )
                        if self._abort:
                            break

                        # Chamber stabilization hold
                        self._set_sub_state("chamber_stabilizing")
                        self._stabilize_counter = 0
                        stab_counter = 0
                        while stab_counter < chamber_stab_time and not self._abort:
                            if self._skip_event.is_set():
                                self._skip_event.clear()
                                break
                            stab_counter += 1
                            self._stabilize_counter = stab_counter
                            await asyncio.sleep(1)

                        if self._abort:
                            break
                    elif prev_chamber > 0:
                        await self._set_chamber_temp(0)

                    prev_chamber = chamber_target
                    prev_level = level

                # ── Heat bed (only up within level) ──
                temps = await self._query_temps()
                if temps["bed"] > bed_target + tolerance:
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

                # ── Smart stabilization ──
                self._set_sub_state("smart_stabilizing")
                notes = await self._smart_stabilize(
                    bed_target, tolerance,
                    smart_min, smart_max, smart_window, smart_threshold,
                )

                if self._abort:
                    break

                # ── Sampling ──
                self._set_sub_state("sampling")
                point = await self._sample_v2(
                    target, sample_count, sample_interval, notes,
                )
                self._points.append(point)

                point_num = idx + 1
                total = len(self._targets)
                log.info(
                    f"✓ #{point_num}/{total} | "
                    f"Bed {bed_target}°C | Chamber {chamber_target}°C | "
                    f"Glass {point['surfaceTemp']}°C | "
                    f"Δ={point['delta']}°C"
                    f"{' | ' + point['notes'] if point.get('notes') else ''}"
                )

                self.server.send_event("calibration:point_collected", {
                    "index": idx,
                    "total": total,
                    "point": point,
                })

                self._save_progress()

                if self._abort:
                    break

            # ── Finish ──
            await self._set_bed_temp(0)
            await self._set_chamber_temp(0)

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
            log.exception("Calibration v2 error")
            self._state = "error"
            self._error = str(e)
            self._sub_state = None
            await self._cooldown()
            self._save_results()

    async def _smart_stabilize(
        self,
        bed_target: float,
        tolerance: float,
        min_wait: int,
        max_wait: int,
        window_size: int,
        threshold: float,
    ) -> str:
        """Smart stabilization: wait for bed_glass to settle.

        Returns notes string: '' if stable, 'slow_stabilize' if timed out.
        """
        glass_history: List[tuple] = []  # (monotonic_time, temperature)
        start = time.monotonic()
        notes = ""
        self._stabilize_counter = 0

        while not self._abort:
            if self._skip_event.is_set():
                self._skip_event.clear()
                break

            temps = await self._query_temps()
            t_bed = temps["bed"]
            t_glass = temps["glass"]
            now = time.monotonic()
            elapsed = int(now - start)
            self._stabilize_counter = elapsed

            # Reset if bed drifted too far from target
            if abs(t_bed - bed_target) > tolerance * 2:
                start = time.monotonic()
                glass_history.clear()
                self._stabilize_counter = 0
                await asyncio.sleep(5)
                continue

            glass_history.append((now, t_glass))

            # Minimum wait not reached
            if elapsed < min_wait:
                await asyncio.sleep(5)
                continue

            # Check glass stability over sliding window
            cutoff = now - window_size
            window = [t for ts, t in glass_history if ts >= cutoff]
            if len(window) >= 10:
                w_range = max(window) - min(window)
                if w_range < threshold:
                    break  # Stable!

            # Timeout
            if elapsed >= max_wait:
                notes = "slow_stabilize"
                log.warning(
                    f"Smart stabilize timeout at {bed_target}°C "
                    f"after {max_wait}s — glass range not within "
                    f"{threshold}°C"
                )
                break

            await asyncio.sleep(5)

        return notes

    async def _sample_v2(
        self,
        target: Dict,
        sample_count: int,
        sample_interval: float,
        stabilize_notes: str,
    ) -> Dict:
        """v2 sampling: collect readings with min/max/notes."""
        beds: List[float] = []
        glasses: List[float] = []
        chambers: List[float] = []

        for _ in range(sample_count):
            if self._abort:
                break
            if self._skip_event.is_set():
                self._skip_event.clear()
                break
            temps = await self._query_temps()
            beds.append(temps["bed"])
            glasses.append(temps["glass"])
            chambers.append(temps["chamber"])
            await asyncio.sleep(sample_interval)

        if not beds:
            beds = [0.0]
            glasses = [0.0]
            chambers = [0.0]

        bed_avg = sum(beds) / len(beds)
        glass_avg = sum(glasses) / len(glasses)
        chamber_avg = sum(chambers) / len(chambers)
        glass_min_val = min(glasses)
        glass_max_val = max(glasses)

        # Build notes
        notes_parts: List[str] = []
        if stabilize_notes:
            notes_parts.append(stabilize_notes)
        if glass_max_val - glass_min_val > 3.0:
            notes_parts.append("unstable")
        if glass_avg > bed_avg:
            notes_parts.append("anomaly")

        return {
            "phase": target["phase"],
            "level": target.get("level"),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bedTarget": target["bed_target"],
            "bedSensorAvg": round(bed_avg, 1),
            "bedSensorMin": round(min(beds), 1),
            "bedSensorMax": round(max(beds), 1),
            "chamberTarget": target["chamber_target"],
            "chamberTemp": round(chamber_avg, 1),
            "surfaceTemp": round(glass_avg, 1),
            "glassMin": round(glass_min_val, 1),
            "glassMax": round(glass_max_val, 1),
            "delta": round(bed_avg - glass_avg, 1),
            "notes": ",".join(notes_parts) if notes_parts else "",
        }

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

        is_v2 = self._config.get("algorithm") == "v2"
        stabilize_time = (
            int(self._config.get("smart_stabilize_min", 300))
            if is_v2 else self.stabilize_time
        )

        remaining = 0
        if self._state == "running" and self._targets:
            remaining_pts = len(self._targets) - self._current_index
            avg_per_point = stabilize_time + 120 + 12
            remaining = remaining_pts * avg_per_point

        # v2: compute current level and total levels from targets
        current_level = None
        total_levels = None
        if is_v2 and self._targets:
            levels_seen = set()
            for t in self._targets:
                lvl = t.get("level")
                if lvl is not None:
                    levels_seen.add(lvl)
            total_levels = len(levels_seen) if levels_seen else None
            if self._current_index < len(self._targets):
                current_level = self._targets[self._current_index].get("level")

        stab_remaining = 0
        if self._sub_state == "stabilizing":
            stab_remaining = max(0, self.stabilize_time - self._stabilize_counter)
        elif self._sub_state == "chamber_stabilizing":
            chamber_stab = int(self._config.get("chamber_stabilize_time", 1200))
            stab_remaining = max(0, chamber_stab - self._stabilize_counter)
        elif self._sub_state == "smart_stabilizing":
            stab_remaining = max(0, self._stabilize_counter)

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
            "stabilizeRemaining": stab_remaining,
            "samplesCollected": 0,
            "currentLevel": current_level,
            "totalLevels": total_levels,
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
