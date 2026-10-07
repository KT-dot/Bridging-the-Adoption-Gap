# Input and metadata formats

These notes describe the actual implementations inspected for this discovery task. They do not introduce a new schema.

## `list.txt`

The primary sample uses UTF-8 rich rows:

```text
project_id | repository_url | deployment_url | interface_type
```

That line describes the format, not a header to insert in the input. Blank lines and lines whose first non-space character is `#` are ignored; inline comments are unsupported. IDs must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. The repository and deployment must be actual locations. The selected interface types are `service_ui`, `public_information_site`, and `data_portal`.

The general parser also accepts simple URL/local-path entries and optional empty rich-row fields. It merges multiple rich rows sharing an ID and reports exact duplicate rows. This study deliberately uses one row and one primary deployment per distinct codebase family, with explicit IDs. Evidence notes belong in the inventory rather than the input rows.

## `metadata.yaml`

Top-level structure:

```yaml
schema_version: 1
projects:
  in-cmd-koha:
    project_name: Centre for Management Development Kerala library catalogue (Koha)
    country: India
    expected_languages: null
    critical_journeys: null
    classification_labels: null
    deployment_repository_relationship:
      status: supplied
      detail: Documented upstream relationship; exact deployed version unknown.
      evidence_url: https://cmd.kerala.gov.in/library/
      deployed_version: null
```

Accepted fields are `project_name`, `classification_labels`, `project_type`, `country`, `government_level`, `operator`, `service_sector`, `intended_audience`, `expected_languages`, `critical_journeys`, `deployment_repository_relationship`, `auth`, `offline_relevant`, `long_process`, `untranslated_allowlist`, and `notes`. YAML `null` preserves unknown values. Supported CSV metadata is an alternative; this dataset uses YAML.

Labels are lists of `{label, source}` objects. Expected languages are documented BCP 47 tags, not inferred from menu labels or repository translation files. Critical journeys require reviewed IDs and action steps; none have been invented during discovery. Auth is left unknown and contains no credentials.

Relationship status must be `supplied`, `verified`, or `unknown`. Here `supplied` means a documented upstream relationship without a verified deployed version/commit; the more precise inventory value is `upstream_relationship_only`. A software footer or HTML generator value is recorded as a self-report, without asserting build equivalence. `verified` is reserved for evidence establishing the specific version relationship.

Validate using `.venv/bin/dpi-audit validate --input list.txt --metadata metadata.yaml`. The CLI checks parser errors and metadata warnings; `build_inventory.py` additionally asserts exact inventory/list/metadata ID equality and unique families and deployments.
