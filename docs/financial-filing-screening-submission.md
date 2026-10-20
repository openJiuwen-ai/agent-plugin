# Financial Filing Screening: submission validation

Version 0.1.2, Apache-2.0. Validated on 2026-10-19.

## Contribution

Adds one skill and one tool for the SEC rendered HTML workflow demonstrated in
the financial workflow versus WorkSwarm comparison. Given a local filing folder,
it extracts financial evidence, computes five financial criteria, assesses two
risk areas and saves a source-cited JSON report. Inspect mode reads the filing
identity and report titles without inference or a financial decision.

The package contains a hash-recorded application snapshot: 19 unchanged source
files and two documented extraction and risk-citation adaptations. Original and
packaged hashes are recorded in source-manifest.json. Research runners, filings,
raw logs, benchmark labels, presentations, model weights, credentials and virtual
environments are excluded from distribution.

## Runtime boundary and dependencies

The host loader was verified in WorkSwarm 0.2.7.beta1 with SDK 0.1.18.post1.
The workflow executes in a separate Python 3.11 environment pinned to
openjiuwen==0.1.17.post1. Its dependencies are declared in runtime/requirements.txt;
maintainer test requirements are outside the plugin package.

The tool has no import-time process, network or file-write side effects. It starts
a fixed worker without a shell, writes only to an explicit new output directory,
refuses external filing references and output overwrites, and returns success=false
on missing dependencies or execution failure. The worker does not inherit cloud
model credentials. Screening uses a separately configured loopback model endpoint.
The plugin does not start model servers or download weights.

## Validation

| Check | Result |
|---|---|
| Full repository suite in isolated SDK environment | 90 passed, including 21 financial plugin tests |
| Default PR quality gate without optional SDK | PASS; 63 passed, 2 SDK-dependent modules skipped |
| Plugin L0/L1 and component import/constructor validation | PASS, no warnings |
| WorkSwarm installation and host load/invoke/unload | PASS with SDK 0.1.18.post1 |
| Actual WorkSwarm chat inspection | PASS; Caterpillar FY2024, 136 reports, zero worker model calls, no decision |
| Actual WorkSwarm chat screening | PASS; installed tool, Qwen3-14B, saved report.json, 389.683-second worker execution |
| Independent report verification | 131 checks passed: values, periods, units, source report IDs, line citations, risk headings and quotes, recalculated criteria and incomplete risk coverage |

The host harness and controlled-model tests are separate from the actual desktop
chat acceptance. Controlled responses test workflow behavior; they do not measure
real model accuracy. Raw acceptance evidence remains local because reports and
SDK logs can contain document content. This is acceptance on one filing, not a
new multi-company accuracy benchmark or performance comparison.

## Accepted live result

The original Caterpillar FY2024 filing produced the expected **REJECT** because
revenue growth was negative. All amounts below are USD millions.

| Criterion | Independently calculated result | Status |
|---|---|---|
| Revenue growth | 64,809 versus 67,060; -3.356695% | FAIL |
| Operating margin | 13,072 / 64,809 = 20.170038% | PASS |
| Operating cash flow | 12,035; R9 line 22 | PASS |
| Liabilities / assets | 68,270 / 87,764 = 77.788159% | PASS |
| Balance sheet | 87,764 - 68,270 - 19,494 = 0 | PASS |

Total equity already includes noncontrolling interests of 3; these are not added
twice. Regulatory coverage remains UNVERIFIED: the environmental-remediation
passage in R31 does not establish overall clearance. Supply-chain risk is FLAG,
supported by the supplier cybersecurity exposure in R36. This is an exposure
statement, not evidence that a cybersecurity incident occurred.

Selected HTML reports cannot establish complete risk coverage. The HTML entry
point therefore retains partial CLEAR assessments as overall UNVERIFIED and
produces REVIEW or REJECT. Missing required financial evidence stops execution.

## Corrections covered by regressions

- Explicitly identify operating cash flow as reported net cash from operating
  activities, retaining exact values, periods, units and citations.
- Scope non-thinking, schema-constrained Qwen3-14B extraction to local requests
  through the SDK extra_body field; verify actual HTTP serialization and the
  cloud credential boundary.
- Set the host deadline to 930 seconds around the bounded 900-second worker and
  cleanup; require successful tool output and a saved report before completion.
- Reset risk headings for each HTML report so adjacent notes do not inherit a
  financial-statement heading; retain existing PDF behavior.
- Prevent incomplete HTML risk coverage from yielding a false overall PASS.
- Supply missing namespace ancestors in the upstream loader unit-test fixture
  when the optional SDK is absent, restoring the default CI path without changing
  production loader behavior.

## Reproduction and publication

Setup, input examples and validation commands are in the plugin README. The
host smoke script is tests/plugins/financial-filing-screening/smoke_host.py;
inspect needs no worker inference, while screen requires the configured model.

Build the distribution with scripts/build.py. The reviewed 0.1.2 ZIP contains
30 files; SHA-256:
`e7a2c73e2ecbbe1be7f89f9a47082364f3ccd4c2777ba5365905acbf54c2b231`.

Submit for review in openJiuwen-ai/agent-plugin. Merging a PR does not publish a
Hub entry; an authorized maintainer subsequently uses scripts/publish.py with a
Hub publishing token. No Hub listing is claimed by this validation record.

- [Contribution process](https://github.com/openJiuwen-ai/agent-plugin/blob/main/CONTRIBUTING.md)
- [Hub publishing process](https://github.com/openJiuwen-ai/agent-plugin/blob/main/docs/hub-publish.md)
