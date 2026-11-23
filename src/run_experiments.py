#!/usr/bin/env python3
"""Run paired KSMAIW experiments on Linux using Python multiprocessing and sched_setaffinity."""
from __future__ import annotations
import argparse
import csv
import json
import math
import multiprocessing as mp
import os
import queue
import random
import signal
import sys
import time
from pathlib import Path

from ksmaiw_core import (
    HysteresisConfig, HysteresisController, apply_process_affinity,
    percentile, read_proc_cpu_seconds, set_affinity, system_snapshot,
)
from workloads import interactive_worker, inference_worker

SCENARIOS = {
    'light': {'base_rps': 65.0, 'peak_rps': 65.0, 'burst_period_s': 1.0, 'burst_s': 0.0,
              'work_units': 5500, 'inference_iterations': 42000, 'slo_ms': 10.0},
    'contended': {'base_rps': 250.0, 'peak_rps': 250.0, 'burst_period_s': 1.0, 'burst_s': 0.0,
                  'work_units': 18500, 'inference_iterations': 65000, 'slo_ms': 12.0},
    'bursty': {'base_rps': 90.0, 'peak_rps': 430.0, 'burst_period_s': 1.8, 'burst_s': 0.9,
               'work_units': 14500, 'inference_iterations': 65000, 'slo_ms': 12.0},
}
POLICIES = ('B0_DEFAULT', 'B1_STATIC', 'B3_KSMAIW')


def write_jsonl(path: Path, rows: list[dict]):
    with path.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + '\n')


def quantile_ms(values):
    return percentile(values, 0.50), percentile(values, 0.95), percentile(values, 0.99), max(values, default=float('nan'))


def current_rate(sc, elapsed):
    if sc['burst_s'] > 0 and (elapsed % sc['burst_period_s']) < sc['burst_s']:
        return sc['peak_rps']
    return sc['base_rps']


def read_cpu_percent(processes, before, wall_s):
    rows = {}
    for name, p in processes.items():
        end = read_proc_cpu_seconds(p.pid)
        start = before.get(name)
        if start is None or end is None or wall_s <= 0:
            rows[name] = None
        else:
            rows[name] = max(0.0, (end - start) / wall_s * 100.0)
    return rows


def run_trial(policy: str, scenario: str, trial: int, out_dir: Path,
              duration_s: float, warmup_s: float, cpu_pair: tuple[int, int],
              load_cpu: int, controller_config: HysteresisConfig) -> dict:
    sc = SCENARIOS[scenario]
    cpu_service, cpu_inference = cpu_pair
    ctx = mp.get_context('fork')
    task_q = ctx.Queue(maxsize=4096)
    result_q = ctx.Queue(maxsize=8192)
    stop_event = ctx.Event()
    inf_counter = ctx.Value('Q', 0, lock=True)

    # The experiment coordinator and open-loop load generator use the isolated load CPU.
    original_affinity = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {load_cpu})
    service_processes = []
    inference = None
    request_rows = []
    controller_rows = []
    submitted = 0
    completed = 0
    timed_out = 0
    generation_drops = 0
    measurement_start_ns = None
    start_ns = 0
    generation_end_ns = 0
    measurement_start_ns = 0
    measurement_end_ns = 0
    end_drain_ns = 0
    pending_issue = {}
    latency_window = []
    last_observation_ns = 0
    window_start_ns = measurement_start_ns
    control_period_ns = int(0.25 * 1e9)
    next_control_ns = measurement_start_ns + control_period_ns
    current_state = 'NORMAL'
    last_borrow_phase = None
    inference_stopped = False
    controller = HysteresisController(controller_config)
    controller_cpu_ns = 0
    controller_checks = 0
    actuation_calls = 0
    burst_slo_violations = 0
    max_queue_depth = 0
    inference_before = 0
    inference_after = 0
    proc_start_cpu = {}
    proc_end_cpu = {}

    try:
        # Start two interactive worker processes, each a single-threaded request executor.
        initial_service_cpus = (cpu_service, cpu_inference) if policy == 'B0_DEFAULT' else (cpu_service,)
        for i in range(2):
            p = ctx.Process(target=interactive_worker,
                            args=(task_q, result_q, initial_service_cpus, stop_event),
                            name=f'ksmaiw-service-{i}')
            p.start()
            service_processes.append(p)
            set_affinity(p.pid, set(initial_service_cpus))
        inference_cpus = (cpu_service, cpu_inference) if policy == 'B0_DEFAULT' else (cpu_inference,)
        inference = ctx.Process(target=inference_worker,
                                 args=(inf_counter, stop_event, sc['inference_iterations'], inference_cpus),
                                 name='ksmaiw-inference')
        inference.start()
        set_affinity(inference.pid, set(inference_cpus))

        # Warm-up is outside the measured interval; no interactive requests are issued yet.
        if warmup_s > 0:
            time.sleep(warmup_s)
        start_ns = time.monotonic_ns()
        measurement_start_ns = start_ns
        measurement_end_ns = start_ns + int(duration_s * 1e9)
        generation_end_ns = measurement_end_ns
        end_drain_ns = measurement_end_ns + int(3.0 * 1e9)
        window_start_ns = start_ns
        last_observation_ns = start_ns
        next_control_ns = start_ns + control_period_ns
        proc_start_cpu = {f'service_{i}': read_proc_cpu_seconds(p.pid) for i, p in enumerate(service_processes)}
        proc_start_cpu['inference'] = read_proc_cpu_seconds(inference.pid)
        inference_before = int(inf_counter.value)

        next_arrival_ns = start_ns
        seq = 0
        now_ns = time.monotonic_ns()
        while now_ns < measurement_end_ns:
            elapsed_s = (now_ns - start_ns) / 1e9
            rate = current_rate(sc, elapsed_s)
            if now_ns >= next_arrival_ns:
                if now_ns >= measurement_start_ns:
                    issue_ns = now_ns
                    item = (seq, issue_ns, sc['work_units'], (seq * 31 + trial * 17) & 0xFFFF)
                    try:
                        task_q.put_nowait(item)
                        submitted += 1
                        pending_issue[seq] = issue_ns
                    except queue.Full:
                        generation_drops += 1
                seq += 1
                next_arrival_ns += int(1e9 / max(rate, 1.0))
            # Drain completed client requests; response timestamp is taken by service worker.
            while True:
                try:
                    rseq, issue_ns, service_start_ns, done_ns, result = result_q.get_nowait()
                except queue.Empty:
                    break
                completed += 1
                pending_issue.pop(rseq, None)
                if issue_ns >= measurement_start_ns:
                    lat = max(0.0, (done_ns - issue_ns) / 1e6)
                    service_ms = (done_ns - service_start_ns) / 1e6
                    queue_ms = max(0.0, lat - service_ms)
                    row = {'policy': policy, 'scenario': scenario, 'trial': trial,
                           'sequence': rseq, 'issue_ns': issue_ns, 'service_start_ns': service_start_ns,
                           'completion_ns': done_ns, 'latency_ms': lat, 'service_time_ms': service_ms,
                           'queue_delay_ms': queue_ms, 'timed_out': False, 'result_checksum': result}
                    request_rows.append(row)
                    latency_window.append((done_ns, lat))
                    last_observation_ns = done_ns
            max_queue_depth = max(max_queue_depth, len(pending_issue))

            # KSMAIW supervisory loop. Only complete, sufficiently fresh windows can switch state.
            if policy == 'B3_KSMAIW' and now_ns >= next_control_ns:
                # One-second rolling telemetry window, evaluated every 250 ms.
                window_cutoff = now_ns - int(1.0e9)
                latency_window = [(ts, value) for ts, value in latency_window if ts >= window_cutoff]
                window_values = [value for _, value in latency_window]
                window_p99 = percentile(window_values, 0.99) if window_values else None
                sample_count = len(window_values)
                age_ms = max(0.0, (now_ns - last_observation_ns) / 1e6)
                before = time.process_time_ns()
                decision = controller.update(window_p99, sample_count, age_ms)
                controller_cpu_ns += time.process_time_ns() - before
                controller_checks += 1
                row = {'elapsed_ms': (now_ns - start_ns) / 1e6,
                       'window_p99_ms': window_p99, 'sample_count': sample_count,
                       'measurement_age_ms': age_ms, 'pending_requests': len(pending_issue),
                       **decision}
                controller_rows.append(row)
                if decision['changed']:
                    current_state = decision['new_state']
                    last_borrow_phase = None
                    # On entering protection, the service can borrow CPU2 during protected slices.
                    if current_state == 'NORMAL':
                        if inference_stopped:
                            os.kill(inference.pid, signal.SIGCONT)
                            inference_stopped = False
                            actuation_calls += 1
                        apply_process_affinity([p.pid for p in service_processes], {cpu_service})
                        actuation_calls += len(service_processes)
                    else:
                        apply_process_affinity([p.pid for p in service_processes], {cpu_service, cpu_inference})
                        actuation_calls += len(service_processes)
                window_start_ns = now_ns
                next_control_ns += control_period_ns

            # Bounded 70/30 borrowing of the inference CPU while in PROTECT.
            # During the borrowed slice inference is stopped; during the remaining slice
            # it runs on CPU2 while interactive workers stay on CPU1.
            if policy == 'B3_KSMAIW' and current_state == 'PROTECT':
                cfg = controller_config
                period_ns = int(cfg.protect_window_ms * 1e6)
                phase_ms = ((now_ns - start_ns) % period_ns) / 1e6
                borrow = phase_ms < cfg.service_borrow_ms
                phase = 'SERVICE_BORROW' if borrow else 'INFERENCE_PROGRESS'
                if phase != last_borrow_phase:
                    t0 = time.process_time_ns()
                    if borrow:
                        if not inference_stopped:
                            os.kill(inference.pid, signal.SIGSTOP)
                            inference_stopped = True
                            actuation_calls += 1
                        apply_process_affinity([p.pid for p in service_processes], {cpu_service, cpu_inference})
                    else:
                        apply_process_affinity([p.pid for p in service_processes], {cpu_service})
                        if inference_stopped:
                            os.kill(inference.pid, signal.SIGCONT)
                            inference_stopped = False
                            actuation_calls += 1
                    controller_cpu_ns += time.process_time_ns() - t0
                    actuation_calls += len(service_processes)
                    controller_rows.append({'elapsed_ms': (now_ns - start_ns) / 1e6,
                                             'event': 'slice_change', 'slice': phase,
                                             'phase_ms': phase_ms, 'state': current_state})
                    last_borrow_phase = phase
            elif policy == 'B3_KSMAIW' and current_state == 'NORMAL' and inference_stopped:
                os.kill(inference.pid, signal.SIGCONT)
                inference_stopped = False
                actuation_calls += 1

            now_ns = time.monotonic_ns()
            if next_arrival_ns > now_ns:
                time.sleep(min(0.001, (next_arrival_ns - now_ns) / 1e9))

        # Snapshot CPU time and inference progress at the measurement boundary (before drain).
        proc_end_cpu = {f'service_{i}': read_proc_cpu_seconds(p.pid) for i, p in enumerate(service_processes)}
        proc_end_cpu['inference'] = read_proc_cpu_seconds(inference.pid)
        inference_after = int(inf_counter.value)
        # Stop generation, then drain queued requests without omitting unfinished requests.
        for _ in service_processes:
            task_q.put(None)
        while time.monotonic_ns() < end_drain_ns and len(pending_issue) > 0:
            while True:
                try:
                    rseq, issue_ns, service_start_ns, done_ns, result = result_q.get(timeout=0.02)
                except queue.Empty:
                    break
                completed += 1
                pending_issue.pop(rseq, None)
                lat = max(0.0, (done_ns - issue_ns) / 1e6)
                service_ms = (done_ns - service_start_ns) / 1e6
                request_rows.append({'policy': policy, 'scenario': scenario, 'trial': trial,
                                     'sequence': rseq, 'issue_ns': issue_ns,
                                     'service_start_ns': service_start_ns, 'completion_ns': done_ns,
                                     'latency_ms': lat, 'service_time_ms': service_ms,
                                     'queue_delay_ms': max(0.0, lat-service_ms),
                                     'timed_out': False, 'result_checksum': result})
        timeout_cutoff = time.monotonic_ns()
        for rseq, issue_ns in list(pending_issue.items()):
            timed_out += 1
            request_rows.append({'policy': policy, 'scenario': scenario, 'trial': trial,
                                 'sequence': rseq, 'issue_ns': issue_ns,
                                 'service_start_ns': None, 'completion_ns': None,
                                 'latency_ms': max(0.0, (timeout_cutoff - issue_ns) / 1e6),
                                 'service_time_ms': None, 'queue_delay_ms': None,
                                 'timed_out': True, 'result_checksum': None})
        pending_issue.clear()
        if inference_stopped:
            os.kill(inference.pid, signal.SIGCONT)
            inference_stopped = False
        stop_event.set()
        inference.join(timeout=2.0)
        if inference.is_alive():
            inference.terminate()
            inference.join(timeout=1.0)
        for p in service_processes:
            p.join(timeout=2.0)
            if p.is_alive():
                p.terminate(); p.join(timeout=1.0)

        wall_s = duration_s
        cpu_secs = {}
        for k, start_cpu in proc_start_cpu.items():
            end_cpu = proc_end_cpu.get(k)
            cpu_secs[k] = (end_cpu - start_cpu) if start_cpu is not None and end_cpu is not None else None
        # Latencies include timed-out requests as censored lower-bound observations.
        lats = [r['latency_ms'] for r in request_rows]
        p50, p95, p99, pmax = quantile_ms(lats)
        good = sum(1 for x in lats if x <= sc['slo_ms'])
        total = len(lats)
        inference_items = max(0, inference_after - inference_before)
        summary = {
            'policy': policy, 'scenario': scenario, 'trial': trial,
            'duration_s': duration_s, 'warmup_s': warmup_s,
            'offered_rate_rps_nominal': sc['base_rps'],
            'offered_rate_peak_rps': sc['peak_rps'], 'slo_ms': sc['slo_ms'],
            'requests_issued': submitted, 'requests_completed': completed,
            'requests_timed_out': timed_out, 'generator_drops': generation_drops,
            'p50_ms': p50, 'p95_ms': p95, 'p99_ms': p99, 'max_ms': pmax,
            'slo_compliance_pct': 100.0 * good / total if total else 0.0,
            'throughput_rps': completed / duration_s,
            'inference_items': inference_items,
            'inference_items_per_s': inference_items / duration_s,
            'controller_checks': controller_checks,
            'controller_transitions': controller.transitions,
            'controller_actuation_calls': actuation_calls,
            'controller_cpu_ms': controller_cpu_ns / 1e6,
            'controller_cpu_pct_of_one_core': 100.0 * controller_cpu_ns / (duration_s * 1e9),
            'max_inflight_requests': max_queue_depth,
            'service_cpu_seconds': sum(v or 0 for k, v in cpu_secs.items() if k.startswith('service_')),
            'inference_cpu_seconds': cpu_secs.get('inference'),
            'service_cpu_pct_one_core': 100.0 * sum(v or 0 for k, v in cpu_secs.items() if k.startswith('service_')) / duration_s,
            'inference_cpu_pct_one_core': 100.0 * (cpu_secs.get('inference') or 0) / duration_s,
            'cpu_set': list(cpu_pair), 'load_generator_cpu': load_cpu,
            'controller_final_state': current_state,
            'latency_window_count': len(controller_rows),
        }
        trial_key = f'{scenario}__{policy}__trial_{trial}'
        raw_dir = out_dir / 'raw'
        raw_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(raw_dir / f'{trial_key}_requests.jsonl', request_rows)
        write_jsonl(raw_dir / f'{trial_key}_controller.jsonl', controller_rows)
        (raw_dir / f'{trial_key}_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        return summary
    finally:
        if inference is not None and inference.is_alive():
            try:
                os.kill(inference.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            inference.terminate(); inference.join(timeout=1.0)
        for p in service_processes:
            if p.is_alive():
                p.terminate(); p.join(timeout=1.0)
        os.sched_setaffinity(0, original_affinity)
        task_q.close(); result_q.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='results')
    ap.add_argument('--duration', type=float, default=3.0, help='measured seconds per trial')
    ap.add_argument('--warmup', type=float, default=0.5)
    ap.add_argument('--trials', type=int, default=3)
    ap.add_argument('--policies', nargs='+', choices=POLICIES, default=list(POLICIES))
    ap.add_argument('--scenarios', nargs='+', choices=list(SCENARIOS), default=list(SCENARIOS))
    ap.add_argument('--seed', type=int, default=20261123)
    ap.add_argument('--service-cpu', type=int, default=None)
    ap.add_argument('--inference-cpu', type=int, default=None)
    ap.add_argument('--load-cpu', type=int, default=None)
    args = ap.parse_args()
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 3:
        raise SystemExit(f'Need at least three allowed CPUs (one loadgen + two workload CPUs); available={allowed}')
    load_cpu = args.load_cpu if args.load_cpu is not None else allowed[0]
    service_cpu = args.service_cpu if args.service_cpu is not None else allowed[1]
    inference_cpu = args.inference_cpu if args.inference_cpu is not None else allowed[2]
    if len({load_cpu, service_cpu, inference_cpu}) != 3 or not set((load_cpu, service_cpu, inference_cpu)).issubset(set(allowed)):
        raise SystemExit(f'CPU selection must be three distinct allowed CPUs. allowed={allowed}')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = system_snapshot()
    env['experiment_parameters'] = vars(args)
    env['selected_cpus'] = {'load_generator': load_cpu, 'service': service_cpu, 'inference': inference_cpu}
    env['scenarios'] = SCENARIOS
    (out / 'environment.json').write_text(json.dumps(env, indent=2), encoding='utf-8')
    print(f"Kernel {env['kernel_release']} | CPUs load/service/inference={load_cpu}/{service_cpu}/{inference_cpu}")
    print('Cgroup snapshot:', env['cgroup'])
    cfg = HysteresisConfig()
    summaries = []
    rng = random.Random(args.seed)
    run_sequence = 0
    policy_orders = {}
    for scenario in args.scenarios:
        for trial in range(1, args.trials + 1):
            order = list(args.policies)
            rng.shuffle(order)
            policy_orders[f'{scenario}__trial_{trial}'] = order
            for order_index, policy in enumerate(order, start=1):
                run_sequence += 1
                print(f'RUN {scenario:10s} {policy:12s} trial {trial}/{args.trials} order={order_index}', flush=True)
                summary = run_trial(policy, scenario, trial, out, args.duration, args.warmup,
                                    (service_cpu, inference_cpu), load_cpu, cfg)
                summary['seed'] = args.seed
                summary['run_sequence'] = run_sequence
                summary['policy_order_index'] = order_index
                summaries.append(summary)
                print(f"  p99={summary['p99_ms']:.3f}ms SLO={summary['slo_compliance_pct']:.1f}% "
                      f"infer={summary['inference_items_per_s']:.1f}/s timeouts={summary['requests_timed_out']}", flush=True)
    env['policy_orders'] = policy_orders
    (out / 'environment.json').write_text(json.dumps(env, indent=2), encoding='utf-8')
    csv_path = out / 'trial_summary.csv'
    if summaries:
        with csv_path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(summaries[0].keys()))
            writer.writeheader(); writer.writerows(summaries)
    (out / 'trial_summary.json').write_text(json.dumps(summaries, indent=2), encoding='utf-8')
    print(f'Wrote {len(summaries)} trials to {csv_path}')


if __name__ == '__main__':
    main()
