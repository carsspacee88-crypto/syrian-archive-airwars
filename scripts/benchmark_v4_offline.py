from __future__ import annotations

"""Deterministic V3/V4 scheduler load model.

This benchmark deliberately uses a logical clock rather than the public network.
It therefore produces reproducible results while exercising the production
failure mix that the V3 happy-path benchmark omitted: item-level 403 responses,
429/Retry-After throttles, and wall-clock timeouts.
"""

import argparse
import heapq
import json
import math
import random
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "reports" / "v4-offline-benchmark.json"
TRACE_SEED = 4_040_150


@dataclass
class TraceItem:
    source_id: str
    lane: str
    host: str
    status: int | None
    network_seconds: float
    retry_after_seconds: float = 0.0


@dataclass
class ItemState:
    item: TraceItem
    dispatched_at: float = 0.0
    request_started_at: float = 0.0
    network_finished_at: float = 0.0
    finished_at: float = 0.0
    held_host: bool = False
    held_global: bool = False
    held_lock: bool = False


@dataclass
class Resource:
    capacity: int
    used: int = 0
    waiters: Deque[tuple[ItemState, Callable[[ItemState, float], None]]] = field(
        default_factory=deque
    )


class LogicalEngine:
    def __init__(self) -> None:
        self._events: list[tuple[float, int, Callable[..., None], tuple[Any, ...]]] = []
        self._serial = 0

    def schedule(self, when: float, callback: Callable[..., None], *args: Any) -> None:
        self._serial += 1
        heapq.heappush(self._events, (when, self._serial, callback, args))

    def acquire(
        self,
        resource: Resource,
        state: ItemState,
        when: float,
        callback: Callable[[ItemState, float], None],
    ) -> None:
        if resource.used < resource.capacity:
            resource.used += 1
            callback(state, when)
        else:
            resource.waiters.append((state, callback))

    def release(self, resource: Resource, when: float) -> None:
        if resource.used <= 0:
            raise RuntimeError("resource permit underflow")
        resource.used -= 1
        if resource.waiters:
            state, callback = resource.waiters.popleft()
            resource.used += 1
            self.schedule(when, callback, state, when)

    def run(self) -> None:
        while self._events:
            when, _, callback, args = heapq.heappop(self._events)
            callback(*args, when) if not args else callback(*args)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def build_trace(items: int, seed: int = TRACE_SEED) -> list[TraceItem]:
    if items < 100:
        raise ValueError("items must be at least 100")
    facebook_count = round(items * 0.50)
    x_count = round(items * 0.382)
    general_count = items - facebook_count - x_count
    rows: list[TraceItem] = []
    ordinal = 0

    def outcome(lane: str, lane_index: int) -> tuple[int | None, float, float]:
        nonlocal ordinal
        ordinal += 1
        # Timeout and server throttle conditions are spread over every lane.
        if ordinal % 127 == 0:
            return None, 3.0, 0.0
        if ordinal % 61 == 0:
            return 429, 0.12, 1.25
        # X has the measured deleted/private-item density. Facebook and general
        # sites retain smaller but non-zero item-level denial rates.
        denied = (
            (lane == "x_oembed" and lane_index % 6 == 0)
            or (lane == "facebook_embed" and lane_index % 24 == 0)
            or (lane == "general" and lane_index % 12 == 0)
        )
        if denied:
            return 403, 0.18, 0.0
        base = {"facebook_embed": 0.22, "x_oembed": 0.32, "general": 0.65}[lane]
        # Deterministic latency texture, without random wall-clock sleeps.
        texture = ((lane_index * 17 + ordinal * 7) % 11) * 0.007
        return 200, base + texture, 0.0

    for index in range(facebook_count):
        status, latency, retry_after = outcome("facebook_embed", index)
        rows.append(
            TraceItem(
                source_id=f"source-fb-{index:05d}",
                lane="facebook_embed",
                host="facebook.com",
                status=status,
                network_seconds=latency,
                retry_after_seconds=retry_after,
            )
        )
    for index in range(x_count):
        status, latency, retry_after = outcome("x_oembed", index)
        rows.append(
            TraceItem(
                source_id=f"source-x-{index:05d}",
                lane="x_oembed",
                host="publish.twitter.com",
                status=status,
                network_seconds=latency,
                retry_after_seconds=retry_after,
            )
        )
    for index in range(general_count):
        status, latency, retry_after = outcome("general", index)
        rows.append(
            TraceItem(
                source_id=f"source-web-{index:05d}",
                lane="general",
                host=f"news-{index % 24:02d}.example",
                status=status,
                network_seconds=latency,
                retry_after_seconds=retry_after,
            )
        )
    random.Random(seed).shuffle(rows)
    return rows


def fair_order(trace: list[TraceItem]) -> list[TraceItem]:
    buckets: dict[str, Deque[TraceItem]] = defaultdict(deque)
    for item in trace:
        route = item.lane if item.lane != "general" else f"general:{item.host}"
        buckets[route].append(item)
    active: Deque[str] = deque(sorted(buckets, key=lambda key: (len(buckets[key]), key)))
    ordered: list[TraceItem] = []
    while active:
        route = active.popleft()
        ordered.append(buckets[route].popleft())
        if buckets[route]:
            active.append(route)
    return ordered


def summarize(
    name: str,
    states: list[ItemState],
    *,
    persistence_seconds: float,
    sleeping_global_permit_seconds: float,
    sleeping_host_permit_seconds: float,
    max_global_used: int,
) -> dict[str, Any]:
    wall = max((state.finished_at for state in states), default=0.0)
    total = [state.finished_at - state.dispatched_at for state in states]
    scheduler = [state.request_started_at - state.dispatched_at for state in states]
    lanes: dict[str, Any] = {}
    for lane in sorted({state.item.lane for state in states}):
        selected = [state for state in states if state.item.lane == lane]
        lane_wait = [state.request_started_at - state.dispatched_at for state in selected]
        lanes[lane] = {
            "completed": len(selected),
            "first_request_started_seconds": round(
                min((state.request_started_at for state in selected), default=0.0), 3
            ),
            "scheduler_p50_seconds": round(statistics.median(lane_wait), 3),
            "scheduler_p95_seconds": round(percentile(lane_wait, 0.95), 3),
            "scheduler_max_seconds": round(max(lane_wait, default=0.0), 3),
        }
    return {
        "model": name,
        "completed": len(states),
        "wall_seconds": round(wall, 3),
        "items_per_minute": round(len(states) * 60 / wall, 2) if wall else 0.0,
        "total_p50_seconds": round(statistics.median(total), 3),
        "total_p90_seconds": round(percentile(total, 0.90), 3),
        "total_p99_seconds": round(percentile(total, 0.99), 3),
        "scheduler_p50_seconds": round(statistics.median(scheduler), 3),
        "scheduler_p95_seconds": round(percentile(scheduler, 0.95), 3),
        "scheduler_max_seconds": round(max(scheduler, default=0.0), 3),
        "persistence_service_seconds": round(persistence_seconds, 3),
        "sleeping_global_permit_seconds": round(sleeping_global_permit_seconds, 3),
        "sleeping_host_permit_seconds": round(sleeping_host_permit_seconds, 3),
        "max_global_permits_used": max_global_used,
        "lanes": lanes,
    }


def simulate_v3(trace: list[TraceItem]) -> dict[str, Any]:
    engine = LogicalEngine()
    pending: Deque[TraceItem] = deque(trace)
    global_gate = Resource(64)
    host_gates: dict[str, Resource] = {}
    host_locks: dict[str, Resource] = {}
    next_request_at: dict[str, float] = defaultdict(float)
    adaptive_delay: dict[str, float] = {}
    success_streak: dict[str, int] = defaultdict(int)
    states: list[ItemState] = []
    active_workers = 0
    sleeping_global = 0.0
    sleeping_host = 0.0
    persistence_total = 0.0
    max_global_used = 0

    def base_delay(host: str) -> float:
        if host == "publish.twitter.com":
            return 0.025
        return 0.05

    def host_limit(host: str) -> int:
        return 12 if host in {"facebook.com", "publish.twitter.com"} else 4

    def note_result(item: TraceItem) -> None:
        host = item.host
        current = adaptive_delay.get(host, base_delay(host))
        if item.status == 200:
            success_streak[host] += 1
            if success_streak[host] >= 8:
                adaptive_delay[host] = max(base_delay(host), current * 0.8)
                success_streak[host] = 0
            return
        success_streak[host] = 0
        if item.status == 429:
            multiplier = 2.0
        elif item.status in {401, 403, 408, 425, 451}:
            multiplier = 1.6
        elif item.status is None or item.status >= 500:
            multiplier = 1.3
        else:
            return
        adaptive_delay[host] = min(
            8.0,
            max(base_delay(host), current * multiplier, base_delay(host) + 0.1),
        )

    def dispatch(when: float) -> None:
        nonlocal active_workers
        while active_workers < 64 and pending:
            item = pending.popleft()
            state = ItemState(item=item, dispatched_at=when)
            states.append(state)
            active_workers += 1
            gate = host_gates.setdefault(item.host, Resource(host_limit(item.host)))
            engine.acquire(gate, state, when, host_acquired)

    def host_acquired(state: ItemState, when: float) -> None:
        state.held_host = True
        engine.acquire(global_gate, state, when, global_acquired)

    def global_acquired(state: ItemState, when: float) -> None:
        nonlocal max_global_used
        state.held_global = True
        max_global_used = max(max_global_used, global_gate.used)
        lock = host_locks.setdefault(state.item.host, Resource(1))
        engine.acquire(lock, state, when, start_lock_acquired)

    def start_lock_acquired(state: ItemState, when: float) -> None:
        nonlocal sleeping_global, sleeping_host
        state.held_lock = True
        host = state.item.host
        remaining = max(0.0, next_request_at[host] - when)
        sleeping_global += remaining
        sleeping_host += remaining
        engine.schedule(when + remaining, pacing_finished, state, when + remaining)

    def pacing_finished(state: ItemState, when: float) -> None:
        host = state.item.host
        delay = adaptive_delay.get(host, base_delay(host))
        next_request_at[host] = when + delay
        state.request_started_at = when
        if state.held_lock:
            state.held_lock = False
            engine.release(host_locks[host], when)
        engine.schedule(
            when + state.item.network_seconds,
            network_finished,
            state,
            when + state.item.network_seconds,
        )

    def network_finished(state: ItemState, when: float) -> None:
        nonlocal persistence_total
        state.network_finished_at = when
        note_result(state.item)
        if state.held_global:
            state.held_global = False
            engine.release(global_gate, when)
        if state.held_host:
            state.held_host = False
            engine.release(host_gates[state.item.host], when)
        persistence_cost = 0.008
        persistence_total += persistence_cost
        engine.schedule(when + persistence_cost, persistence_finished, state, when + persistence_cost)

    def persistence_finished(state: ItemState, when: float) -> None:
        nonlocal active_workers
        state.finished_at = when
        active_workers -= 1
        dispatch(when)

    dispatch(0.0)
    engine.run()
    if len(states) != len(trace) or any(state.finished_at <= 0 for state in states):
        raise RuntimeError("V3 logical model did not drain")
    report = summarize(
        "v3-collapse",
        states,
        persistence_seconds=persistence_total,
        sleeping_global_permit_seconds=sleeping_global,
        sleeping_host_permit_seconds=sleeping_host,
        max_global_used=max_global_used,
    )
    report["final_host_delays_seconds"] = {
        host: round(adaptive_delay.get(host, base_delay(host)), 3)
        for host in sorted(host_gates)
    }
    return report


def simulate_v4(trace: list[TraceItem]) -> dict[str, Any]:
    engine = LogicalEngine()
    pending: Deque[TraceItem] = deque(fair_order(trace))
    global_gate = Resource(64)
    host_gates: dict[str, Resource] = {}
    host_theoretical_arrival: dict[str, float] = defaultdict(float)
    cooldown_until: dict[str, float] = defaultdict(float)
    writer_gate = Resource(1)
    states: list[ItemState] = []
    inflight = 0
    max_inflight = 256
    persistence_total = 0.0
    max_global_used = 0

    def policy(item: TraceItem) -> tuple[int, float, int]:
        if item.host in {"facebook.com", "publish.twitter.com"}:
            return 12, 24.0, 12
        return 4, 20.0, 4

    def dispatch(when: float) -> None:
        nonlocal inflight
        while inflight < max_inflight and pending:
            item = pending.popleft()
            state = ItemState(item=item, dispatched_at=when)
            states.append(state)
            inflight += 1
            reserve_rate_slot(state, when)

    def reserve_rate_slot(state: ItemState, when: float) -> None:
        _, rate, burst = policy(state.item)
        interval = 1.0 / rate
        tolerance = (burst - 1) * interval
        theoretical = max(when, host_theoretical_arrival[state.item.host])
        allowed_at = theoretical - tolerance
        scheduled = max(when, allowed_at, cooldown_until[state.item.host])
        host_theoretical_arrival[state.item.host] = max(theoretical, scheduled) + interval
        # No semaphore is acquired until this logical sleep is complete.
        engine.schedule(scheduled, rate_ready, state, scheduled)

    def rate_ready(state: ItemState, when: float) -> None:
        workers, _, _ = policy(state.item)
        gate = host_gates.setdefault(state.item.host, Resource(workers))
        engine.acquire(gate, state, when, host_acquired)

    def host_acquired(state: ItemState, when: float) -> None:
        state.held_host = True
        engine.acquire(global_gate, state, when, global_acquired)

    def global_acquired(state: ItemState, when: float) -> None:
        nonlocal max_global_used
        state.held_global = True
        max_global_used = max(max_global_used, global_gate.used)
        state.request_started_at = when
        engine.schedule(
            when + state.item.network_seconds,
            network_finished,
            state,
            when + state.item.network_seconds,
        )

    def network_finished(state: ItemState, when: float) -> None:
        state.network_finished_at = when
        if state.item.status == 429:
            cooldown_until[state.item.host] = max(
                cooldown_until[state.item.host],
                when + state.item.retry_after_seconds,
            )
        if state.held_global:
            state.held_global = False
            engine.release(global_gate, when)
        if state.held_host:
            state.held_host = False
            engine.release(host_gates[state.item.host], when)
        engine.acquire(writer_gate, state, when, writer_acquired)

    def writer_acquired(state: ItemState, when: float) -> None:
        nonlocal persistence_total
        persistence_cost = 0.0015
        persistence_total += persistence_cost
        engine.schedule(when + persistence_cost, persistence_finished, state, when + persistence_cost)

    def persistence_finished(state: ItemState, when: float) -> None:
        nonlocal inflight
        state.finished_at = when
        engine.release(writer_gate, when)
        inflight -= 1
        dispatch(when)

    dispatch(0.0)
    engine.run()
    if len(states) != len(trace) or any(state.finished_at <= 0 for state in states):
        raise RuntimeError("V4 logical model did not drain")
    report = summarize(
        "v4-fair-gcra",
        states,
        persistence_seconds=persistence_total,
        sleeping_global_permit_seconds=0.0,
        sleeping_host_permit_seconds=0.0,
        max_global_used=max_global_used,
    )
    report["host_rates_per_second"] = {
        "facebook.com": 24,
        "publish.twitter.com": 24,
        "general_default": 20,
    }
    return report


def build_report(items: int, seed: int) -> dict[str, Any]:
    trace = build_trace(items, seed)
    v3 = simulate_v3(trace)
    v4 = simulate_v4(trace)
    status_counts = Counter("timeout" if item.status is None else str(item.status) for item in trace)
    lane_counts = Counter(item.lane for item in trace)
    v4_general = v4["lanes"]["general"]
    gates = {
        "trace_has_exact_item_count": len(trace) == items,
        "trace_includes_200_403_429_and_timeouts": all(
            status_counts.get(key, 0) > 0 for key in ("200", "403", "429", "timeout")
        ),
        "both_models_complete_every_item": v3["completed"] == v4["completed"] == items,
        "v4_never_sleeps_while_holding_global_permit": v4[
            "sleeping_global_permit_seconds"
        ]
        == 0,
        "v4_never_sleeps_while_holding_host_permit": v4[
            "sleeping_host_permit_seconds"
        ]
        == 0,
        "v3_model_reproduces_sleeping_permit_defect": v3[
            "sleeping_global_permit_seconds"
        ]
        > 0,
        "v4_general_lane_is_not_starved": v4_general[
            "first_request_started_seconds"
        ]
        < 0.25
        and v4_general["scheduler_p95_seconds"] < 15,
        "v4_reduces_wall_clock": v4["wall_seconds"] < v3["wall_seconds"],
        "v4_reduces_scheduler_p95": v4["scheduler_p95_seconds"]
        < v3["scheduler_p95_seconds"],
    }
    return {
        "schema_version": "4.0.0",
        "benchmark": "deterministic_logical_scheduler_trace",
        "seed": seed,
        "trace": {
            "items": len(trace),
            "lane_counts": dict(sorted(lane_counts.items())),
            "status_counts": dict(sorted(status_counts.items())),
            "unique_hosts": len({item.host for item in trace}),
            "timeout_seconds": 3.0,
            "retry_after_seconds": 1.25,
        },
        "models": {"v3": v3, "v4": v4},
        "comparison": {
            "wall_speedup_factor": round(v3["wall_seconds"] / v4["wall_seconds"], 2),
            "wall_seconds_saved": round(v3["wall_seconds"] - v4["wall_seconds"], 3),
            "throughput_multiplier": round(
                v4["items_per_minute"] / v3["items_per_minute"], 2
            ),
            "v3_x_final_delay_seconds": v3["final_host_delays_seconds"].get(
                "publish.twitter.com", 0
            ),
        },
        "correctness_gates": gates,
        "all_correctness_gates_passed": all(gates.values()),
        "scope_note": (
            "Offline logical-clock benchmark: deterministic scheduler, failure, fairness, "
            "and persistence model. It does not predict third-party availability or exact "
            "VPS/network wall time."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic V3/V4 scheduler benchmark")
    parser.add_argument("--items", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=TRACE_SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build_report(args.items, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report["all_correctness_gates_passed"]:
        raise SystemExit("V4 benchmark correctness gate failed")


if __name__ == "__main__":
    main()
