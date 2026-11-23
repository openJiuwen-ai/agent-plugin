# Evaluation reference

Shared reference for both ways of using Observability and Evaluation: in the
JiuwenSwarm (i.e., HyperAgent) chat app ([guide](JIUWENSWARM_PLUGIN_GUIDE.md)) and
as a standalone module for OpenJiuwen applications
([guide](AGENT_FRAMEWORK_PLUGIN_GUIDE.md)).

**Contents:** [Judge providers](#judge-providers) ·
[Strategies and taxonomies](#strategies-and-taxonomies) ·
[Command-line options](#command-line-options) ·
[Results and history](#results-and-history) ·
[The evaluation UI service](#the-evaluation-ui-service) ·
[Remote access](#remote-access) ·
[Data, privacy, and cost](#data-privacy-and-cost) ·
[Known limitations](#known-limitations) ·
[Troubleshooting](#troubleshooting) · [Glossary](#glossary)

## Judge providers

The judge is the LLM that reviews a trajectory. Its credentials are separate from
the credentials of the agents being evaluated.

| `--client` | API key environment variable | Endpoint | Default model |
| --- | --- | --- | --- |
| `openai` (default) | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `gpt-5.5` |
| `openrouter` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` | `openai/gpt-5.5` |
| `mlops` | `MLOPS_API_KEY` | internal MLOps service | `DeepSeek-V4-Flash-W8A8` |

Choose a model your account can access with `--model-id`. In the UI, the same
settings are entered in **Setup**.

**Loading keys from a `.env` file.** The CLI reads only environment variables.
To export a `.env` file into the current shell:

```bash
set -a; source <(tr -d '\r' < /path/to/.env); set +a
```

`tr -d '\r'` removes Windows line endings. Without it, a `.env` saved on
Windows leaves an invisible `\r` at the end of the key, and every request fails
with `APIConnectionError`.

## Strategies and taxonomies

| Strategy | Taxonomy | What the judge does | Judge calls |
| --- | --- | --- | --- |
| `taxonomy-guided` (default) | `ours` (default): the **RAMS** failure taxonomy | Checks each step against each failure category | One per top-level category (7 for RAMS) |
| `taxonomy-guided` | `mast`: the **MAST** multi-agent failure taxonomy | Same, with MAST categories | One per top-level category |
| `zero-shot` | none | Finds failures without a predefined list | One |

Few-shot evaluation is not available yet.

## Command-line options

```bash
ojw_evaluation run failure-detector --trajectory PATH [options]
```

`python -m ojw_evaluation.cli` is equivalent.

| Option | Meaning | Default |
| --- | --- | --- |
| `--trajectory PATH` | Trajectory to evaluate. Without it, the CLI asks interactively. | — |
| `--client` | `openai`, `openrouter`, `mlops` | `openai` |
| `--model-id` (or `--model`) | Judge model | the provider's default |
| `--strategy` | `taxonomy-guided`, `zero-shot` | `taxonomy-guided` |
| `--taxonomy` | `ours` (RAMS), `mast` | `ours` |
| `--temperature`, `--top-p` | Sampling settings; ignored for models that don't accept them | provider default |
| `--dedup` | Merge duplicate findings: `none`, `exact`, `by_code` | `none` |
| `--output-dir DIR` | Save results under `DIR` instead of next to the trajectory | next to the trajectory |
| `--print-traces` | Print the evaluator's own trace spans | off |

| Exit code | Meaning |
| --- | --- |
| `0` | Evaluation completed |
| `130` | Interrupted with Ctrl+C (recorded as interrupted) |
| other non-zero | Evaluation failed or is incomplete; see `run.log` |

Any file with supported trajectory contents can be evaluated; it doesn't have to
be named `agentic_trajectory.json`.

## Results and history

Each evaluation (from the CLI or the UI) gets its own folder next to the
evaluated file:

```text
<run folder>/
├── agentic_trajectory.json            # or dialogue.json in JiuwenSwarm
└── evaluation/
    └── 2026-11-06_13-52-49_terminal/  # UTC time; _terminal or _ui
        ├── evaluation.json            # status, parameters, error message
        ├── parameters.json            # judge settings (never the API key)
        ├── source.json                # path and checksum of the evaluated file
        ├── trajectory.json            # exact snapshot that was evaluated
        ├── failure_detector_result.json
        └── <timestamp>/failure-detector/<strategy>/<run name>/
            ├── run.log                # log of this evaluation: check this first
            ├── annotations.json       # the findings
            └── prompts and raw judge replies
```

- **Findings** (`annotations.json`) list each failure with its step, category,
  evidence and rationale. A step without findings is not proof that the step is
  correct.
- **Partial results** are marked as such; missing annotations are reported, not
  hidden.
- **History:** the UI's **Previous evaluations** lists UI and terminal runs for
  the selected file. Press **Refresh history** after a terminal run. Runs saved
  elsewhere with `--output-dir` are not listed.
- **Download:** the UI's **Download failure_detector_results.json** button
  downloads the findings.
- **Uploads:** files uploaded in the UI are stored in the service's upload area;
  Setup shows the exact path.

## The evaluation UI service

The UI is served by a small local service that starts on first use and keeps
running after your application or JiuwenSwarm exits. Its state lives in an
`evaluation_ui/` folder:

| Setup | `evaluation_ui/` location |
| --- | --- |
| Standalone (`open_evaluation_ui`) | `<workspace>/evaluation_ui/` (`workspace` argument) |
| JiuwenSwarm | `<agent workspace>/evaluation_ui/`; also recorded as `service_root` in each dialogue's `metadata.json` |

**Check or stop the service:**

```bash
# Standalone installation
ojw-evaluation-ui --output-dir /path/to/evaluation_ui --status    # prints the URL
ojw-evaluation-ui --output-dir /path/to/evaluation_ui --stop

# JiuwenSwarm installation (no standalone commands): use the host's Python
"$SWARM_DIR/.venv/bin/python" -I "$OJW_PLUGIN_DIR/rails/evaluation/service_process.py" \
  --output-dir /path/to/evaluation_ui --status          # or --stop
```

Stopping cancels evaluations that are running in the UI. Saved results and
history survive restarts. After updating the plugin, stop the service: it
restarts with the new code on next use (refreshing the browser is not enough).

## Remote access

The UI listens on `127.0.0.1` only. When Python runs on a remote machine:

- **SSH:** on your local computer, forward the port shown in the URL:
  ```bash
  ssh -N -L 8765:127.0.0.1:8765 user@remote-host
  ```
  JiuwenSwarm also needs its frontend port (for example `-L 5173:127.0.0.1:5173`).
- **VS Code Remote:** open the **Ports** panel → **Forward a Port** → enter the
  port. If VS Code assigns a different local port, change only the port in the
  URL.

Always open the **complete** URL, including everything after `#`
(`#v=setup&token=…`). The token grants access to the UI: don't share it or post
it publicly.

## Data, privacy, and cost

- **What is sent:** the evaluated trajectory, including prompts, model outputs,
  tool inputs and outputs, and any user data they contain, is sent to the judge
  provider you choose. Evaluate only data you're allowed to share with that
  provider.
- **What is stored:** results, prompts and raw judge replies are written next to
  the evaluated file. API keys are never written to result files.
- **Cost:** `taxonomy-guided` makes one judge call per taxonomy category (7 for
  RAMS), each containing the full trajectory; `zero-shot` makes one. Large
  trajectories therefore cost proportionally more.
- **Size limit:** the UI accepts inputs up to **10 MiB**.

## Known limitations

- Linux or WSL only (Unix file locking).
- Only `invoke()` is recorded, not `stream()`.
- JiuwenSwarm: `/evaluate` must be typed; it doesn't appear in the slash menu.

## Troubleshooting

When an evaluation fails, check `evaluation.json` (field `error`) and `run.log`
in its result folder first. For security, the CLI doesn't print provider error
messages, because they can echo the API key.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Run failed (ValueError)` within a second | API key variable not set in **this** shell (the CLI doesn't read `.env`) | `export OPENROUTER_API_KEY=…` (matching `--client`), or load `.env` as shown [above](#judge-providers) |
| `Run failed (APIConnectionError)` after many instant retries | Key contains `\r` from a Windows-format `.env` | Load with `tr -d '\r'`, or convert the file once: `sed -i 's/\r$//' .env` |
| `Run failed (APIConnectionError)` after slow retries | No network route to the provider (proxy, firewall, TLS inspection) | Test with `curl https://openrouter.ai/api/v1/models`; set `HTTPS_PROXY` / CA bundle as your network requires |
| `Run failed (AuthenticationError / NotFoundError / PermissionDeniedError)` | Invalid key, or the model isn't available to your account | Check the key and `--model-id` |
| UI page opens but access is refused or no file is selected | The `#…` part of the URL was lost | Open the complete URL |
| UI link doesn't load at all on a remote machine | Port not forwarded | See [Remote access](#remote-access) |
| UI still shows old behavior after an update | The service still runs the old code | Stop the service, then open the UI again |
| A terminal run is missing from **Previous evaluations** | History not refreshed, or saved with `--output-dir` | Press **Refresh history**; avoid `--output-dir` if you want UI history |

## Glossary

| Term | Meaning |
| --- | --- |
| **JiuwenSwarm (i.e., HyperAgent)** | The chat application. Installed as the `workswarm` Python package; its commands start with `jiuwenswarm-`. |
| **OpenJiuwen (OJW)** | The agent framework that JiuwenSwarm and your own agents are built on. |
| **Rail** | An OpenJiuwen hook object attached to an agent with `register_rail`. `ObservabilityRail` records; `EvaluationRail` manages JiuwenSwarm dialogues. |
| **Trajectory** | The structured record of a run: every agent invocation, model call, and tool call with timing and token counts. |
| **Dialogue** | In JiuwenSwarm, the trajectory of one chat session (`dialogue.json`). Its exchanges share one session history, so it is exported with `system_architecture: shared_context`. |
| **Judge** | The LLM that evaluates a trajectory. |
| **RAMS** (`ours`) | The plugin's own failure taxonomy for multi-agent systems. |
| **MAST** | Multi-Agent System failure Taxonomy, a published taxonomy of multi-agent failure modes. |
| **Taxonomy-guided / zero-shot** | The judge checks a fixed list of failure categories / finds failures without one. |
