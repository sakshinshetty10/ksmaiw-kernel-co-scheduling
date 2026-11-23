# KSMAIW Python/Linux Implementation — README

## 1. Project overview

This project is a Python implementation prototype for **Kernel-Level Co-Scheduling of AI Inference and Interactive Workloads Under Tail-Latency SLOs (KSMAIW)**.

The implementation studies whether client-observed latency from an interactive workload can guide CPU allocation when an independent CPU-intensive workload is running at the same time. It compares a shared-CPU baseline, a static CPU-affinity baseline, and a deterministic feedback controller.

**Implementation language:** Python 3.10+  
**Target platform:** Linux (the experiment runner uses Linux process CPU affinity and POSIX signals).  
**C source files:** None. The Linux interfaces are accessed from Python's standard library.

This is a prototype evaluation harness. The interactive and inference programs are synthetic CPU workloads; they are not a production Redis server or a trained neural-network inference engine. The implementation uses process affinity as its primary enforcement mechanism. An optional cgroup-v2 helper is provided for a writable, delegated cgroup subtree.

## 2. Project structure

After extracting the implementation ZIP, the files are arranged as follows:

```text
KSMAIW/
├── src/
│   ├── ksmaiw_core.py
│   ├── cgroup_v2.py
│   ├── workloads.py
│   ├── run_experiments.py
│   └── analyze_results.py
├── tests/
│   └── test_controller.py
└── validate.py
```

### File descriptions

| File | Purpose |
|---|---|
| `src/ksmaiw_core.py` | Implements CPU-work helper functions, Linux affinity operations, `/proc` CPU-time sampling, system snapshots, percentile calculation, and the hysteresis controller. |
| `src/cgroup_v2.py` | Optional cgroup-v2 backend. Creates a group under an explicitly supplied delegated subtree, configures cpuset and CPU weight, moves PIDs, and changes CPU weight. It does not modify the cgroup mount automatically. |
| `src/workloads.py` | Contains the synthetic interactive request worker and the CPU-intensive inference-surrogate worker. |
| `src/run_experiments.py` | Runs the B0, B1 and B3 policies across the configured workload scenarios and repeated trials. Writes raw request/controller logs and trial summaries. |
| `src/analyze_results.py` | Reads trial summaries and raw controller logs, calculates aggregate metrics, and generates CSV/Markdown tables and PNG figures. |
| `tests/test_controller.py` | Unit tests for the controller state machine, including threshold persistence and stale/insufficient observations. |
| `validate.py` | Runs implementation checks and validates the output structure/data where applicable. |

## 3. Scheduling policies compared

### B0 — Shared CPU / default Linux scheduling

The interactive workers and inference-surrogate process are allowed to execute on the same selected pair of CPUs. Linux's normal scheduler decides which runnable process executes. No feedback-based CPU-affinity changes are made by KSMAIW.

### B1 — Static CPU-affinity partition

The interactive workers are pinned to the selected service CPU, while the inference-surrogate process is pinned to the selected inference CPU. The partition remains fixed throughout the trial. There is no feedback controller.

### B3 — KSMAIW deterministic adaptive policy

The controller observes completed interactive-request latency. It calculates a rolling p99 estimate and checks it periodically. When high latency persists for the configured number of valid observation windows, it enters `PROTECT`. In that state, the service may temporarily borrow the inference CPU. The inference process is paused during the service-borrow portion and resumed during the inference-progress portion. The policy returns to `NORMAL` only after the lower threshold has been satisfied for the configured number of windows.

The controller uses two thresholds (enter and exit) rather than one threshold. This hysteresis, together with persistence checks, reduces rapid switching around a single boundary. Measurements with too few samples or excessive age are not used to trigger a transition.

Default controller configuration in `ksmaiw_core.py`:

- Enter protection when rolling p99 is at least **8 ms**.
- Leave protection when rolling p99 is at most **5 ms**.
- Require **2** consecutive valid high windows to enter protection.
- Require **3** consecutive valid low windows to return to normal.
- Require at least **20** samples in the latency window.
- Reject measurements older than **750 ms**.
- Use a **100 ms** protection cycle, with **70 ms** service borrowing and the remaining **30 ms** for inference progress.

These are prototype configuration values, not universal or hard real-time guarantees.

## 4. Workload scenarios

The experiment runner defines three synthetic scenarios in `SCENARIOS` within `src/run_experiments.py`.

| Scenario | Arrival pattern | Purpose |
|---|---|---|
| `light` | Approximately constant, low offered request rate | Checks controller and policy behaviour when the interactive workload is not heavily contended. |
| `contended` | Approximately constant, higher offered request rate | Creates sustained CPU competition between interactive work and inference work. |
| `bursty` | Alternates between a lower base rate and a higher burst rate | Tests whether latency feedback can react to transient overload and recover. |

The interactive worker performs a deterministic CPU task for each submitted request. The inference-surrogate worker continuously performs a separate deterministic CPU-bound task and increments a completed-item counter. This makes the two workloads reproducible without external model files or network services.

## 5. Metrics collected

The runner records per-request observations and trial-level metrics. Important metrics include:

| Metric | Meaning |
|---|---|
| p50 / p95 / p99 latency | Percentiles of client-observed request completion latency, measured from request issue to worker completion. |
| Maximum latency | Highest observed request latency in the measured trial. |
| SLO compliance | Percentage of completed measured requests at or below the scenario's predeclared latency threshold. |
| Request throughput | Completed interactive requests per second over the measurement interval. |
| Inference items/s | Completed inference-surrogate work items per second. This is useful-work progress, not CPU utilization. |
| Service / inference CPU percentage | Approximate process CPU time divided by wall-clock time, expressed as a percentage of one CPU core. |
| Controller CPU overhead | CPU time consumed by controller checks and actuation, normalized to one core over the trial interval. |
| Controller transitions | Number of changes between `NORMAL` and `PROTECT`. |
| Timed-out requests / generator drops | Requests that did not complete within the allowed drain interval, and requests not submitted because the bounded task queue was full. |
| Queue delay | Client-observed latency minus measured service execution time. |
| Measurement age | Age of the latest completed request observation when a controller decision is evaluated. |

Latency percentiles are calculated from individual request observations. Repeated trial runs, rather than individual requests, should be treated as the independent units when interpreting variation between policies.

## 6. Requirements

- Linux kernel and Python **3.10 or newer**.
- Permission to set process CPU affinity for child processes (normally available to the same user for processes it owns).
- At least three CPUs available to the process for the default experiment layout: one load-generator/coordinator CPU, one service CPU, and one inference CPU. If fewer CPUs are available, the experiment runner may reject the default CPU selection or the comparison may no longer match the intended isolation design.
- For analysis and plots: `pandas` and `matplotlib`.
- For the optional cgroup-v2 backend: a cgroup-v2 mount and a writable delegated subtree with the relevant controllers enabled. Root access alone does not guarantee that a container has a writable cgroup hierarchy.

Install analysis dependencies in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install pandas matplotlib
```

The workload runner and controller primarily use the Python standard library. The analysis script requires the two packages listed above.

## 7. How to run the experiments

Open a terminal in the extracted project directory. Run the experiment from the project root so that the runner can import the modules from `src` as designed.

```bash
python3 src/run_experiments.py --out results --duration 3 --warmup 0.5 --trials 3
```

This runs all three policies (`B0_DEFAULT`, `B1_STATIC`, `B3_KSMAIW`) over all three scenarios (`light`, `contended`, `bursty`) for three trials each. That produces 27 policy/scenario/trial combinations.

For a shorter smoke test:

```bash
python3 src/run_experiments.py --out smoke_results --duration 1 --warmup 0.2 --trials 1
```

To run only selected policies or scenarios:

```bash
python3 src/run_experiments.py --out selected_results --duration 3 --warmup 0.5 --trials 3 --policies B0_DEFAULT B1_STATIC --scenarios light contended
```

To see all available command-line options:

```bash
python3 src/run_experiments.py --help
```

The runner selects CPUs from the process's allowed CPU set by default. If needed, specify explicit CPU IDs that are actually allowed to the current process:

```bash
python3 src/run_experiments.py --out results --service-cpu 1 --inference-cpu 2 --load-cpu 0
```

Replace `0`, `1`, and `2` with valid CPU IDs shown by:

```bash
taskset -pc $$
```

Do not assume that the machine's logical CPU numbers are available inside a container; use the allowed CPU list reported by the command and by the experiment environment record.

## 8. Analyze results and generate figures

After the experiment runner finishes, run:

```bash
python3 src/analyze_results.py --results results --out analysis
```

The analysis directory contains:

```text
analysis/
├── figures/
│   ├── figure_1_p99_latency.png
│   ├── figure_2_slo_compliance.png
│   ├── figure_3_inference_progress.png
│   ├── figure_4_p95_vs_p99.png
│   ├── figure_5_controller_overhead.png
│   └── figure_6_controller_trace.png
└── tables/
    ├── aggregate_metrics.csv
    ├── aggregate_metrics.md
    ├── trial_level_metrics.csv
    ├── scenario_definitions.csv
    ├── scenario_definitions.md
    ├── controller_activity.csv
    ├── controller_activity.md
    ├── environment.csv
    └── environment.md
```

Some outputs are conditional on the corresponding raw controller log being present. Keep the original `results` directory: it contains the underlying records needed to regenerate the analysis.

## 9. Output and raw data

The experiment runner writes a `results` directory containing, among other files:

- `trial_summary.csv` — one row per trial with aggregate performance metrics.
- `environment.json` — kernel, Python, allowed CPU set and cgroup information captured from the execution host.
- `raw/` — JSON Lines request-level and controller-level logs for individual trials.

Do not edit raw logs manually. If an experiment is rerun, use a new output directory or archive the previous output directory so that results from different configurations are not mixed.

## 10. Unit tests and validation

Run the controller tests with:

```bash
python3 -m unittest discover -s tests -v
```

Run the project validator with:

```bash
python3 validate.py
```

A successful test run checks the controller's expected state transitions and basic project/data validation. It does not prove that the implementation is correct for every Linux kernel, CPU topology, or production workload.

## 11. Optional cgroup-v2 integration

`src/cgroup_v2.py` provides an optional helper for a **delegated writable cgroup-v2 subtree**. It intentionally refuses to create groups directly under the cgroup mount root. A caller must explicitly provide a delegated directory below `/sys/fs/cgroup`.

The helper supports creating a group, setting `cpuset.mems`, `cpuset.cpus` and `cpu.weight`, moving a PID into the group, updating CPU weight, and removing groups created by that helper. It does not automatically create delegation, enable controllers in parent cgroups, or move the current process. Those steps require host-specific administrative configuration and should be performed only on a test machine with appropriate permissions.

The experiment runner in this package uses Linux process affinity and POSIX signals for its baseline/adaptive comparisons. The presence of `cgroup_v2.py` should not be interpreted as proof that the reported experiments used cgroup enforcement.

## 12. Safety and operational notes

- Run experiments on a machine where CPU-intensive tests are permitted. They can temporarily consume substantial CPU resources.
- Do not run on a production host or a shared server without permission.
- The B3 controller sends `SIGSTOP` and `SIGCONT` to its own inference worker process to implement bounded inference-progress slices. It does not signal unrelated processes.
- The runner uses child-process affinity and restores the coordinator process's original affinity when the trial exits normally or through its cleanup path.
- Avoid running multiple copies of the experiment simultaneously; concurrent runs contaminate CPU measurements.
- The cgroup helper is opt-in and checks for a writable delegated subtree before modifying it. Inspect the target path before use.

## 13. Reproducibility recommendations

For a report or submission, record:

1. CPU model, logical CPU count, allowed CPU list, RAM, kernel release and Python version.
2. Whether execution occurred on bare metal, a virtual machine, or a container; include cgroup quota and cpuset details.
3. The exact command line, policy list, scenario list, duration, warm-up, number of trials and random seed.
4. The complete raw `results` directory and generated `analysis` directory.
5. Any changes to scenario rates, work units, SLO thresholds or controller configuration.
6. Whether optional cgroup-v2 enforcement was actually enabled and which delegated path/controller settings were used.

Keep the configuration fixed across compared policies. If the machine is shared or heavily loaded, repeat the experiment in randomized policy order and report the host conditions. Three repetitions are suitable for an initial project prototype, but more independent runs are preferable for strong statistical conclusions.

## 14. Limitations

- The workloads are synthetic and do not reproduce Redis networking, storage, locking, or a real neural inference runtime.
- Python CPU-bound loops are not representative of optimized native inference kernels and are affected by interpreter overhead.
- Process CPU affinity controls where a process may run; it is not a guaranteed CPU-time reservation and does not isolate cache, memory bandwidth, interrupts, or NUMA effects.
- The adaptive controller reacts to observed latency and can respond to causes other than CPU scheduling contention.
- The percentile estimate depends on the sample count and window size; short trials can produce noisy tail estimates.
- The B3 time-sliced borrowing mechanism is a prototype control policy, not a kernel scheduler and not a hard real-time guarantee.
- Results from one host/container do not establish general performance across machines.

## 15. Correct interpretation of results

The implementation is intended to test the mechanics of the proposed design and produce a repeatable comparison under controlled synthetic conditions. A lower p99 value alone does not establish that a policy is better: interactive SLO compliance, inference completions, controller overhead, transition count, dropped requests and host constraints must be considered together.

Do not describe synthetic inference-surrogate completions as neural-network inferences. Do not describe process-affinity enforcement as a custom Linux scheduler. Do not claim cgroup-v2 or sched_ext results unless those mechanisms were actually enabled and measured. State the tested environment and limitations alongside every result.

---

**Project:** KSMAIW — Kernel-Level Co-Scheduling of AI Inference and Interactive Workloads Under Tail-Latency SLOs  
**Implementation:** Python control and workload processes using Linux scheduling interfaces  
**Purpose:** Academic prototype and reproducible experimental evaluation
