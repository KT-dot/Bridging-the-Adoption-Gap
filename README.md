# Quantitative accessibility and UI/UX audit code

This folder contains the quantitative audit implementation, detectors, aggregation/report generation, CLI, configuration, 76-feature draft codebook, fixtures, regression tests, dependency locks, repository-cloning scripts and run-verification helpers.

The current implementation is version 0.1.1. The completed ten-project India pilot used 0.1.0. The historical source snapshot is retained under `results/scalability-validation-20261007/baseline/`; exact pilot inputs are under `results/india-pilot/inputs/`. No downloaded government repositories, observations, screenshots, generated reports, literature or manuscript files are included. Historical local-path inputs describe the original machine and must be adapted before reuse.

## Setup

Run from this folder (Python 3.11–3.13 and Node/npm are needed):

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e '.[test]'
npm ci
.venv/bin/python -m playwright install chromium
```

## Validate and run

```sh
.venv/bin/dpi-audit validate --input list.txt --metadata metadata.yaml
.venv/bin/dpi-audit codebook validate
.venv/bin/python -m pytest tests -q -rxX
.venv/bin/dpi-audit audit --input list.txt --output results/new-run --mode hybrid --config config/default.yaml --metadata metadata.yaml --resume
```

The supplied `list.txt` has the ten accepted repository/deployment pairs. A new row can use `project-id | repository-URL-or-local-path | deployment-URL | service_ui`; paths relative to list.txt are supported. Use the default config for remote acquisition; `config/india_local_pilot.yaml` disables remote cloning.

## Extra helpers and limits

`scripts/` contains cloning, historical pilot monitoring and final report scripts. Verification/benchmark helpers are preserved in their original directory layout beneath `results/`. Saved-run verification and synthetic report benchmarks require the original pilot data, intentionally omitted here. The local hybrid smoke helper uses supplied fixtures and expected outcomes. No experiments were rerun while preparing this upload folder.

Two known detector limitations (NAV-12 request errors and NAV-14 unspecified journeys) remain documented as expected failures in `tests/test_behavior.py`. Software tests do not establish research measurement validity. Codebook review, human validation and final literature-to-indicator alignment remain pending. Source patterns do not prove deployed functioning or adoption outcomes.

See `docs/input_format.md`, `docs/methodology.md`, `docs/scalability.md` and `docs/original_workspace_README.md` for original documentation. References there to excluded workspace research/results files require the original workspace.

`UPLOAD_MANIFEST.json` records copied-source provenance. The portable fixture input and expected-only assertion file are documented packaging adaptations. No code/config changes were made to the original workspace.
