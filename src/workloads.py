#!/usr/bin/env python3
"""Synthetic interactive key-value workload and CPU inference surrogate."""
from __future__ import annotations
import os
import queue
import signal
import time

from ksmaiw_core import cpu_work


def interactive_worker(task_queue, result_queue, worker_cpus: tuple[int, ...], stop_event):
    os.sched_setaffinity(0, set(worker_cpus))
    while True:
        item = task_queue.get()
        if item is None:
            break
        seq, issue_ns, work_units, key = item
        start_ns = time.monotonic_ns()
        # Small dictionary-style operation followed by deterministic CPU work.
        value = ((key * 2654435761) ^ (key >> 3)) & 0xFFFFFFFF
        result = cpu_work(work_units, value)
        end_ns = time.monotonic_ns()
        result_queue.put((seq, issue_ns, start_ns, end_ns, result))


def inference_worker(shared_counter, stop_event, iterations: int, cpus: tuple[int, ...]):
    os.sched_setaffinity(0, set(cpus))
    seed = 17
    while not stop_event.is_set():
        cpu_work(iterations, seed)
        seed = (seed + 1) & 0xFFFF
        with shared_counter.get_lock():
            shared_counter.value += 1
