#!/usr/bin/env python3
"""Aggregate trial-level outcomes and generate manuscript-ready figures/tables."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

METRICS = [
    'p50_ms', 'p95_ms', 'p99_ms', 'max_ms', 'slo_compliance_pct',
    'throughput_rps', 'inference_items_per_s', 'controller_cpu_pct_of_one_core',
    'controller_transitions', 'requests_timed_out', 'generator_drops',
    'service_cpu_pct_one_core', 'inference_cpu_pct_one_core'
]


def dump_table(df: pd.DataFrame, csv_path: Path, md_path: Path):
    df.to_csv(csv_path, index=False)
    md_path.write_text(df.to_markdown(index=False, floatfmt='.3f'), encoding='utf-8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--results', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    source = Path(args.results)
    out = Path(args.out)
    figures = out / 'figures'
    tables = out / 'tables'
    figures.mkdir(parents=True, exist_ok=True)
    tables.mkdir(parents=True, exist_ok=True)
    trials = pd.read_csv(source / 'trial_summary.csv')
    environment = json.loads((source / 'environment.json').read_text())
    trials['policy'] = pd.Categorical(trials['policy'], ['B0_DEFAULT', 'B1_STATIC', 'B3_KSMAIW'], ordered=True)
    trials['scenario'] = pd.Categorical(trials['scenario'], ['light', 'contended', 'bursty'], ordered=True)

    agg_rows = []
    for (scenario, policy), group in trials.groupby(['scenario', 'policy'], observed=True):
        row = {'scenario': scenario, 'policy': policy, 'n_trials': len(group)}
        for metric in METRICS:
            row[f'{metric}_mean'] = group[metric].mean()
            row[f'{metric}_sd'] = group[metric].std(ddof=1) if len(group) > 1 else 0.0
            row[f'{metric}_median'] = group[metric].median()
        agg_rows.append(row)
    aggregate = pd.DataFrame(agg_rows)
    dump_table(aggregate, tables / 'aggregate_metrics.csv', tables / 'aggregate_metrics.md')
    trials.to_csv(tables / 'trial_level_metrics.csv', index=False)

    # Table: workload scenarios.
    config = environment.get('scenarios', {})
    scenario_rows = []
    for name, sc in config.items():
        scenario_rows.append({'scenario': name, 'base_rps': sc['base_rps'],
            'peak_rps': sc['peak_rps'], 'burst_period_s': sc['burst_period_s'],
            'burst_s': sc['burst_s'], 'work_units_per_request': sc['work_units'],
            'inference_iterations_per_item': sc['inference_iterations'], 'slo_ms': sc['slo_ms']})
    scenario_table = pd.DataFrame(scenario_rows)
    dump_table(scenario_table, tables / 'scenario_definitions.csv', tables / 'scenario_definitions.md')

    # Table: controller state and switching counts.
    ctrl_rows = []
    raw = source / 'raw'
    for path in sorted(raw.glob('*_controller.jsonl')):
        # filenames contain scenario and policy; retain trial id
        parts = path.name.replace('_controller.jsonl', '').split('__')
        if len(parts) != 3 or parts[1] != 'B3_KSMAIW':
            continue
        scenario, policy, trial_name = parts
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        state_rows = [r for r in rows if 'new_state' in r]
        slice_rows = [r for r in rows if r.get('event') == 'slice_change']
        ctrl_rows.append({'scenario': scenario, 'trial': trial_name,
            'checks': len(state_rows),
            'state_transitions': sum(bool(r.get('changed')) for r in state_rows),
            'protect_windows': sum(r.get('new_state') == 'PROTECT' for r in state_rows),
            'service_borrow_slices': sum(r.get('slice') == 'SERVICE_BORROW' for r in slice_rows),
            'inference_progress_slices': sum(r.get('slice') == 'INFERENCE_PROGRESS' for r in slice_rows)})
    controller_table = pd.DataFrame(ctrl_rows)
    if not controller_table.empty:
        dump_table(controller_table, tables / 'controller_activity.csv', tables / 'controller_activity.md')

    # Figure 1: p99 latency, mean +/- SD.
    for metric, label, fname, ylim in [
        ('p99_ms', 'Client p99 latency (ms)', 'figure_1_p99_latency.png', None),
        ('slo_compliance_pct', 'Requests within declared SLO (%)', 'figure_2_slo_compliance.png', (0, 105)),
        ('inference_items_per_s', 'Completed inference-surrogate items/s', 'figure_3_inference_progress.png', None),
        ('controller_cpu_pct_of_one_core', 'Controller CPU time (% of one core)', 'figure_5_controller_overhead.png', None),
    ]:
        fig, ax = plt.subplots(figsize=(8.6, 4.8))
        scenarios = list(trials['scenario'].cat.categories)
        policies = list(trials['policy'].cat.categories)
        x = list(range(len(scenarios)))
        width = 0.23
        for j, policy in enumerate(policies):
            means, errs = [], []
            for scenario in scenarios:
                g = trials[(trials.scenario == scenario) & (trials.policy == policy)][metric]
                means.append(g.mean() if len(g) else float('nan'))
                errs.append(g.std(ddof=1) if len(g) > 1 else 0)
            offsets = [v + (j - 1) * width for v in x]
            ax.bar(offsets, means, width=width, yerr=errs, capsize=3, label=policy)
        ax.set_xticks(x, scenarios)
        ax.set_ylabel(label)
        ax.set_xlabel('Workload scenario')
        ax.grid(axis='y', alpha=0.25)
        ax.legend(frameon=False, loc='upper left', bbox_to_anchor=(1.02, 1.0))
        if ylim:
            ax.set_ylim(*ylim)
        fig.tight_layout(rect=[0, 0, 0.82, 1])
        fig.savefig(figures / fname, dpi=200)
        plt.close(fig)

    # Figure 4: p95/p99 paired points for individual trials.
    fig, ax = plt.subplots(figsize=(7.4, 5.2))
    markers = {'B0_DEFAULT': 'o', 'B1_STATIC': 's', 'B3_KSMAIW': '^'}
    for policy in trials['policy'].cat.categories:
        g = trials[trials.policy == policy]
        ax.scatter(g['p95_ms'], g['p99_ms'], marker=markers[str(policy)], s=55, label=str(policy), alpha=.85)
    ax.set_xlabel('p95 latency (ms)')
    ax.set_ylabel('p99 latency (ms)')
    ax.set_title('Tail-latency relationship across repeated trial runs')
    ax.grid(alpha=.25)
    ax.legend(frameon=False)
    fig.tight_layout(); fig.savefig(figures / 'figure_4_p95_vs_p99.png', dpi=200); plt.close(fig)

    # Figure 6: controller telemetry/state trace for the first bursty B3 trial.
    candidates = sorted(raw.glob('bursty__B3_KSMAIW__trial_*_controller.jsonl'))
    if candidates:
        rows = [json.loads(x) for x in candidates[0].read_text().splitlines() if x.strip()]
        evals = [r for r in rows if 'new_state' in r]
        if evals:
            fig, ax = plt.subplots(figsize=(9.0, 4.7))
            xs = [r['elapsed_ms'] / 1000 for r in evals]
            ys = [r['window_p99_ms'] for r in evals]
            ax.plot(xs, ys, marker='o', linewidth=1.5, label='Rolling p99')
            ax.axhline(8.0, linestyle='--', linewidth=1, label='Enter threshold')
            ax.axhline(5.0, linestyle=':', linewidth=1, label='Exit threshold')
            ax.set_xlabel('Time from measured run start (s)')
            ax.set_ylabel('Rolling client p99 (ms)')
            ax.set_title('KSMAIW controller telemetry (bursty scenario, one trial)')
            ax.grid(alpha=.25); ax.legend(frameon=False)
            fig.tight_layout(); fig.savefig(figures / 'figure_6_controller_trace.png', dpi=200); plt.close(fig)

    # Environment table.
    cg = environment.get('cgroup', {})
    env_table = pd.DataFrame([
        {'field': 'Kernel', 'value': environment.get('kernel_release')},
        {'field': 'Python', 'value': environment.get('python')},
        {'field': 'Allowed CPUs', 'value': ','.join(map(str, environment.get('allowed_cpus', [])))},
        {'field': 'cpuset.cpus.effective', 'value': cg.get('cpuset.cpus.effective')},
        {'field': 'cpu.max', 'value': cg.get('cpu.max')},
        {'field': 'cpu.weight', 'value': cg.get('cpu.weight')},
        {'field': 'cgroup writable', 'value': cg.get('cgroup_v2_writable')},
        {'field': 'Execution type', 'value': 'Containerized Linux environment; host details limited'},
    ])
    dump_table(env_table, tables / 'environment.csv', tables / 'environment.md')

    # Concise data-derived notes with guardrails around n.
    lines = ['# Experimental analysis summary', '',
        f"Trials: {len(trials)}; scenarios: {', '.join(map(str, trials['scenario'].cat.categories))}; policies: {', '.join(map(str, trials['policy'].cat.categories))}.",
        'The run-level trial is the unit of aggregation. Requests within a run are not treated as independent replicates.',
        'Error bars in figures are one standard deviation across trials; n is small and no superiority hypothesis test is claimed.', '',
        '## Aggregate p99 and SLO results', '', aggregate[['scenario','policy','n_trials','p99_ms_mean','p99_ms_sd','slo_compliance_pct_mean','slo_compliance_pct_sd','inference_items_per_s_mean','inference_items_per_s_sd']].to_markdown(index=False, floatfmt='.3f'), '',
        '## Interpretation', '',
        '- Compare latency and inference completions together; a p99 reduction obtained by suppressing inference is a trade-off, not a free gain.',
        '- B3 may remain in NORMAL under light or non-persistent spikes because the hysteresis rule requires two qualifying windows and adequate fresh samples.',
        '- These measurements apply only to the recorded synthetic workloads and this execution environment.',
    ]
    (out / 'analysis_summary.md').write_text('\n'.join(lines), encoding='utf-8')
    print(f'Wrote analysis to {out}; aggregate groups={len(aggregate)}')


if __name__ == '__main__':
    main()
