# Source-plus-browser interface audit methodology

This document describes the implemented auditor. Run-specific parameters, versions,
collection dates and codebook status are appended by the report generator. A pilot
with an unfrozen codebook is provisional and requires researcher validation.

## Units and sampling

One project is one codebase with its declared representative deployed interface.
Source interfaces, pages, viewports and interaction states are nested observations,
not independent projects. Related codebases require family-level review. A discovery
sample does not support population-wide prevalence or causal adoption claims.

The browser starts from the supplied route and samples eligible internal links using
configured journey, service, form, help, contact and navigation priorities. The page
cap is an upper bound, not a promise of full coverage. Record sampled, rendered,
failed, blocked and skipped pages separately. Critical journeys and expected languages
remain unknown when not documented in metadata.

## Collection and provenance

Static inspection reads bounded first-party source files, excluding configured
vendored/generated content. Repository code is never executed. Source revision,
dirty status, file coverage and exclusions are recorded. A source checkout is not
assumed to match the deployed version. Local initialized submodules may contribute
files when they fall within the configured inspection scope.

Runtime collection uses Playwright/Chromium and axe-core with desktop/mobile
viewports and configured keyboard, reflow, zoom, text-spacing, motion, language,
form and resource checks. Exact tool versions, viewports, limits and skipped states
are recorded in the run and project inventories. Screenshots, bounded DOM evidence,
raw axe results and observations support review. Automation and sampled checks do
not establish full WCAG conformance or the quality of authenticated journeys.

Respect robots rules and configured per-host delays. Do not bypass authentication
or submit real applications. Mutating requests and unsafe routes are blocked by the
default research configuration. Those safeguards can limit rendering and must be
reported as collection limitations. Review/redact evidence before public release.

## Feature coding

The versioned YAML codebook defines applicability, detector methods, evidence,
aggregation and automation limits. WCAG-related checks and broader UX indicators
are separate labels. Search, language controls, breadcrumbs and offline support are
not universal requirements. Heuristic detection requires human review.

Availability has four values: `present`, `not_found_in_scope`, `unknown`, and
`not_applicable`. Not-found means not observed in the inspected scope, not proven
absent. Verification is separately `pass`, `partial`, `fail`, or `not_tested`.
Static presence does not establish runtime operation. For barrier-oriented features,
presence describes the detected barrier; it is not a desirable-feature score.

Aggregation follows the codebook and the recorded implementation. Preserve page
heterogeneity, method-specific availability, tested/eligible counts and coverage
limits. Review minimum-coverage behavior at both interface and project level during
the pilot before treating aggregates as validated measurements.

## Descriptive reporting and missingness

For each feature, observed availability is `present / (present + not_found_in_scope)`.
The verification pass rate is `pass / (pass + partial + fail)`. Report numerator,
denominator and all category counts, including unknown, not-applicable and not-tested,
plus assessment coverage among enrolled projects. Empty denominators have no rate.
Keep runtime-assessed and source-only measurements distinct.

Category tables describe feature profiles, not an overall compliance score. Metadata
comparisons require known fields and adequate groups. Usage statistics require a
matching service unit, definition and time window; visits, registrations or transaction
totals are not interchangeable measures of inclusive adoption.

## Validation and reproducibility

Preserve original automated observations, reviewer judgments and adjudications
separately. The generated stratified review queue contains tasks, not completed human
validation. Agreement/precision/recall require suitable independently reviewed data.
Do not freeze a codebook with a fabricated approval note. Revise/version rules after
pilot review and rerun affected projects before a main study.

The run manifest records input/metadata hashes, semantic configuration, source
revisions, codebook/code hashes, scheduling and checkpoint decisions. Per-project
checkpoints support resumption. Rebuild reports from preserved project outputs with
`dpi-audit report --results <directory>`; do not silently mix incompatible versions.

This missing report-support document was supplied on 7 October 2026, before the
first pilot report was generated, from the inspected implementation and the existing
`dpi_adoption_paper/04_methodology/quantitative_protocol.md`. Detector code and
codebook rules were not changed by this documentation repair.
