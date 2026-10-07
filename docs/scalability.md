# Scalability changes and tested scope

Version 0.1.1 was prepared after the October 2026 India pilot, which used 0.1.0. The pilot's detector definitions, observations and results were not changed. Capacity improvements do not establish detector accuracy or adoption effects.

## Changes

- Report observations and measurements are read incrementally. Measurement and page CSVs no longer require one batch-wide list. When columns are discovered dynamically, the CSV writer spools records to disk, preserving late columns, field order and existing cell representations.
- Worker futures are submitted through a queue bounded by configured worker capacity, rather than submitting every project at once.
- Workers start in owned process groups on POSIX. A hard timeout terminates the worker group, including browser children that ignore the first termination signal. The Linux regression test exercised that failure case.
- Resume retries `failed` and `complete_with_errors` checkpoints when `run.retry_failed_on_resume` is true. Successful matching checkpoints remain skipped. Setting this existing option false retains failed/partial checkpoints explicitly.
- The manifest records completed projects incrementally, so completed outcomes remain inspectable if a later worker or the parent is interrupted.
- Configuration validation rejects nonpositive worker/time budgets. Fingerprints include the new software version, preventing silent reuse of old checkpoints as new-version measurements.

## Evidence from local tests

The saved checks are in `results/scalability-validation-20261007/`:

| Check | Completed result | Interpretation |
|---|---|---|
| 100 synthetic reports, before/after renderer | 67,420 observations; 7,600 project-feature rows and 15,200 interface rows; all nine CSVs byte-identical | Reporting capacity and output preservation; ten saved profiles repeated, not new government data |
| Peak report memory | Baseline 156,100 KiB; updated 99,924 KiB, about 36% lower | One local process measurement; not a universal memory bound |
| Report time | Baseline 4.026 s; updated 4.309 s | One local run; memory reduction involved extra disk I/O, so no speedup is claimed |
| Actual static CLI on 100 fixture IDs | All 100 completed with four workers in approximately 22 s | Small shared local source fixture; not representative of 100 large repositories or websites |
| Resume | All 100 matching successful checkpoints skipped | Checkpoint behavior verified |
| Partial-retry policy | One deliberately injected partial checkpoint reran; other 99 skipped | Synthetic failure-case test, not an observed government-service failure |
| Updated hybrid CLI | Five local fixture projects completed; ten known-behavior assertions passed, including correctly blocked robots access | Browser/source/report smoke test, not validation of all 76 features |
| Regression tests | 14 passed; two known detector defects reproduced as expected failures | Infrastructure correctness checks plus selected behaviors; NAV-12/NAV-14 issues remain |
| Historical-report reconstruction | All feature rows and nine report tables reproduced; HTML feature table matched; historical files unchanged during the check | New reporting infrastructure preserved historical measurements |

Exact details: `report_benchmark.json`, `execution_checks.json`, `combined_tests.log`, `hybrid_smoke_assertions.json` and `historical_rebuild_check.log`. The pre-change source is archived under `baseline/` with a hash manifest. Synthetic data are labelled fixture and stay outside the main research sample.

## Running a larger collection

Use a separate output directory for the new version. For the existing local checkouts:

```sh
.venv/bin/dpi-audit audit --input results/india-pilot/inputs/list.local.txt --output results/india-v011 --mode hybrid --config config/india_local_pilot.yaml --metadata metadata.yaml --workers 4 --resume
```

This command is provided for a future collection; it was **not run on government sites** for the scalability work. The input still contains ten accepted projects. Increasing input size requires independently qualifying additional codebases. `--workers` sets simultaneous project workers, not pages or observations. The cross-process host delay and robots behavior remain in place; do not increase request rates to manufacture throughput. Two workers remain the default; choose concurrency using available memory, CPU and measured browser behavior.

The fixture and report benchmarks can be repeated with:

```sh
.venv/bin/python -m pytest tests/test_scalability.py results/code-verification-20261007/test_behavior.py -q -rxX
.venv/bin/python results/scalability-validation-20261007/benchmark_reports.py
```

For new collection measurements, record the new version, configuration, codebook and checkpoints. Do not overwrite the 0.1.0 pilot to imply that these changes were used originally. After changing a detector, version it and rerun affected measurements in a new run.

## Limits

This is a single-machine batch runner, not a distributed service. Full browser collection on 100 real government deployments was not benchmarked. Browser duration, server restrictions, repository sizes and screenshot storage can dominate cost. Source-revision checks during planning remain sequential. Report feature/inventory metadata still grow with project count, and per-project page arrays are loaded one project at a time. Streaming reduces measurement-table memory; it does not make total memory constant. Remote clone contention and parent-interruption behavior were not exhaustively stress-tested. POSIX process-group cleanup was tested on Linux; equivalent Windows process-tree cleanup is not implemented.

The codebook remains draft. The known link-request and missing-journey defects, source test/example evidence and measurement-validation tasks remain distinct from scalability and were not silently fixed here.
