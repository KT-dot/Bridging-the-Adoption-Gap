# Government interface accessibility/UX research

This workspace contains an implemented audit runner and an India-only discovery dataset. The current research input contains **10 accepted distinct codebase families**, **53 candidate records**, and **2 additional deployments**. It does not meet the target of approximately 100 verified codebases. No full accessibility audit was run during discovery.

Start with [discovery_methodology.md](discovery_methodology.md) for evidence criteria, limitations and collection history, and [discovery_status.md](discovery_status.md) for completed work and remaining work.

The later [India pilot](results/india-pilot/README.md) processed the ten accepted projects using version 0.1.0. Version 0.1.1 adds bounded worker scheduling, process-group timeout cleanup, partial-run retries and streaming report exports. See [scalability changes and tests](docs/scalability.md); the 100-project workloads there are synthetic software tests, not additional accepted government codebases. The pilot remains unchanged. For interpreting the research, see [how the methods support recommendations](dpi_adoption_paper/04_methodology/recommendation_research/methodology_to_recommendations.md).

## Research files

| File | Purpose |
| --- | --- |
| `list.txt` | Accepted source/interface pairs consumed by the audit |
| `government_deployment_inventory.csv` | Evidence, licenses, operators, languages, access observations and family decisions |
| `metadata.yaml` | Supported auditor metadata, joined by project ID |
| `additional_deployments.csv` | Verified alternatives within already accepted families |
| `candidate_queue.csv` | Missing evidence, restricted/inaccessible interfaces and excluded leads |
| `discovery/staging/in_completion/` | Authoritative reviewed records used to rebuild these files |
| `discovery/evidence_cache/` | Cached public-source responses and their SHA-256 index |
| `discovery/browser_observations/` | Read-only browser observations, public HTML and available screenshots |
| `discovery/validation_report.json` | Counts, hashes and data-integrity checks |

## Validate without auditing

Run from this workspace root using the existing environment:

```sh
.venv/bin/dpi-audit validate --input list.txt --metadata metadata.yaml
.venv/bin/dpi-audit codebook validate
.venv/bin/python discovery/tools/build_inventory.py
```

The last command rebuilds final data files from the reviewed completion batch and checks IDs, family uniqueness and joins. It does not browse or audit websites. Input and metadata formats are documented in [docs/input_format.md](docs/input_format.md); their implementations are `src/dpi_audit/inputs.py` and `src/dpi_audit/metadata.py`.

The supplied codebook is version 1.0.0 with 76 features. It validates, but its status is **draft** and it has no freeze lock. A pilot, detector/manual-review calibration and a reviewed codebook freeze remain necessary before a comparative study. Dataset collection is separate from those steps.

## Start the existing audit when ready

```sh
.venv/bin/dpi-audit audit --input list.txt --output results/india --mode hybrid --config config/default.yaml --metadata metadata.yaml --resume
```

This is the actual supported command, provided for later execution. It acquires source repositories and runs runtime/static checks. The current upstream repository versions generally have no proven correspondence to deployed builds; keep source and deployment findings qualified accordingly. Public pages were researched without credentials or real application submissions.

## Resume discovery

Review the candidate queue and existing evidence before making requests. Add newly reviewed records to the completion batch, preserve rejected/withdrawn decisions, and rebuild with `discovery/tools/build_inventory.py`. The older `in_*` batch logs are research history; their provisional acceptance decisions are superseded by the completion batch. The `_superseded_scope_change` directory contains out-of-scope non-Indian work and is not part of this dataset.

`discovery/tools/verify_batch.py` accepts JSON request plans with `project_id`, `purpose`, `url`, optional `text`, and `grep` (a pattern or list of patterns). It records HTTP status, redirect destinations, timestamps and evidence-cache paths. `observe_interfaces.py` performs public-page observation with Chromium, without invoking the auditor. Both tools require network access and respect the discovery fetcher's per-host delays and robots restrictions. The browser deliberately blocks non-GET/HEAD/OPTIONS requests, service workers, and downloads; this can limit SPA rendering and must not be mistaken for ordinary-user service availability.
