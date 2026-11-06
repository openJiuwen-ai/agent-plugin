# Observability and evaluation tests

All runtime tests load `plugins/observability-and-evaluation/` from this checkout.
They do not import the framework's applications or read its generated runs.
Model responses come from local fakes or a loopback HTTP provider.

From `agent-plugin/`, using Python 3.11 on Linux:

```bash
python -m pip install uv
uv pip install --python .venv/bin/python pytest build 'hatchling>=1.27,<2' \
  -r plugins/observability-and-evaluation/requirements.txt
.venv/bin/python -m pytest tests/plugins/observability-and-evaluation --require-ojw-runtime -ra
```

Create `.venv` with `uv venv --python 3.11 .venv` first if needed. The required
runtime option fails on missing dependencies instead of accepting skipped runtime
checks. With only pytest installed, the repository's usual `scripts/pr_check.py`
still runs lightweight package checks and skips dependency-backed tests.

| Location | Coverage |
| --- | --- |
| `test_runtime_package.py` | Manifest/structure validation, ZIP contents, application-import exclusion. |
| `test_runtime_compatibility.py` | Explicit paths, missing dependencies, read-only imports/session output, and host namespace cleanup. |
| `test_packaging.py` | Wheel/editable/source-archive installs outside the checkout, public import identity and console commands, rebuild without source checkout, artifact/symlink exclusions. |
| `runtime/` | Observation lifecycle, trace export, dialogue merging and isolation, `/evaluate`, interrupted tools, startup/unload races, UI HTTP/worker behavior, offline CLI evaluation, taxonomies, and result/history records. |
| `runtime/test_packaged_runtime.py` | Build and extract the actual ZIP, make its files read-only, run the full SDK workflow without standalone aliases, and verify installed files are unchanged. |
| `packaged_smoke.py` | Standalone runner for the full ZIP lifecycle, native Tool, no-inference `/evaluate`, local judge, workflow UI, and unload. Optional real WorkSwarm envelopes. |

The ported tests originate from the framework's `tests/unit/` and
`tests/ojw_evaluation/` at commit `ba3f182ed23967f248e63b77705c898fdd2fe7eb`.
Application scenario tests and Playwright/browser-harness scripts were not copied.
The selected HTTP and worker tests cover evaluation behavior without a browser
dependency. Fixture details are in [fixtures/README.md](fixtures/README.md).

Runtime imports are bound directly to the destination files by
`runtime/conftest.py`; application imports fail explicitly. Child-process tests
bootstrap those same paths. Installation tests separately exercise installed
packages, and the ZIP smoke uses an isolated interpreter that does not process
the developer environment's editable-install aliases.

CI jobs in `.github/workflows/pr-quality.yml` keep these environments separate:

- The existing lightweight quality gate installs pytest only.
- The runtime/standalone job installs core requirements and build tools, requires
  the plugin suite, and builds both standalone distribution formats.
- The host job installs WorkSwarm `0.2.7` plus plugin requirements, validates L2,
  builds/extracts the ZIP, and runs `packaged_smoke.py` with
  `--host-envelope --interaction --move-workspace`.

The host smoke runner is [packaged_smoke.py](packaged_smoke.py).
