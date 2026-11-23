# JiuwenSwarm guide: evaluate your chat dialogues

This guide installs **Observability and Evaluation** into JiuwenSwarm (i.e.,
HyperAgent), records a chat dialogue, and evaluates it in the bundled web UI.
Everything the plugin needs (recorder, evaluator, UI, taxonomies) ships inside
it; no UI build is required.

> **Naming.** JiuwenSwarm (i.e., HyperAgent) is the chat application. It is
> installed as the Python package `workswarm`, and its commands start with
> `jiuwenswarm-`. It is built on the OpenJiuwen (OJW) agent framework.
>
> Building your own OpenJiuwen application instead of using the chat app? Follow
> the [Agent Framework guide](AGENT_FRAMEWORK_PLUGIN_GUIDE.md). Judge providers,
> result files, remote access and shared troubleshooting are in the
> [Evaluation reference](EVALUATION_REFERENCE.md).

**Contents:** [1. Requirements](#1-requirements) ·
[2. Set your paths](#2-set-your-paths) ·
[3. Prepare the JiuwenSwarm environment](#3-prepare-the-jiuwenswarm-environment) ·
[4. Get the plugin](#4-get-the-plugin) · [5. Install the plugin](#5-install-the-plugin) ·
[6. Evaluate a dialogue](#6-evaluate-a-dialogue) ·
[7. Where files are saved](#7-where-files-are-saved) ·
[8. Upgrade or uninstall](#8-upgrade-or-uninstall) ·
[9. Troubleshooting](#9-troubleshooting)

## 1. Requirements

| | |
| --- | --- |
| OS | Linux (Bash) or WSL |
| Python | 3.11 |
| JiuwenSwarm | `workswarm` `0.2.7` |
| Framework | OpenJiuwen `0.1.19`, OpenTelemetry `1.41.1` (installed in step 3) |
| Tools | [`uv`](https://docs.astral.sh/uv/) (or use equivalent `python -m pip` commands) |
| Judge | An API key for one judge provider: OpenAI, OpenRouter, or MLOps |

Other JiuwenSwarm versions have not been tested.

## 2. Set your paths

Replace the two paths with your own, and run these lines in every new terminal
you use for this guide:

```bash
export PLUGIN_REPO=/absolute/path/to/agent-plugin      # this repository
export SWARM_DIR=/absolute/path/to/jiuwenswarm         # your JiuwenSwarm installation
export JIUWENSWARM_DATA_DIR="$SWARM_DIR/data"
export OJW_PLUGIN_DIR="$JIUWENSWARM_DATA_DIR/agent/workspace/plugins/plugin_packages/local/observability-and-evaluation"
```

`PLUGIN_REPO` is only needed if you build the plugin from source (step 4C) or
install its dependencies from the checkout (step 3).

## 3. Prepare the JiuwenSwarm environment

Do **either 3A or 3B**, then **3C**.

### 3A. You already have JiuwenSwarm

Check the installation and stop it before changing its dependencies. Replace
`default` if your instance has another name.

```bash
test -x "$SWARM_DIR/.venv/bin/python" && echo "python: ok"
test -f "$JIUWENSWARM_DATA_DIR/config/config.yaml" && echo "data dir: ok"
"$SWARM_DIR/.venv/bin/python" -I -c 'from importlib.metadata import version; print(version("workswarm"))'
"$SWARM_DIR/.venv/bin/jiuwenswarm-start" --stop default
```

Expected: `python: ok`, `data dir: ok`, and `0.2.7`.

### 3B. New JiuwenSwarm installation

Create the environment and install JiuwenSwarm:

```bash
mkdir -p "$SWARM_DIR"
uv venv --python 3.11 "$SWARM_DIR/.venv"
uv pip install --python "$SWARM_DIR/.venv/bin/python" 'workswarm==0.2.7'
```

Initialize the data directory (existing files are never overwritten):

```bash
"$SWARM_DIR/.venv/bin/python" -I - <<'PYTHON'
import os
from pathlib import Path
from jiuwenswarm.common.utils import get_user_workspace_dir, prepare_workspace

root = Path(os.environ["JIUWENSWARM_DATA_DIR"]).resolve()
assert Path(get_user_workspace_dir()).resolve() == root, "JIUWENSWARM_DATA_DIR is not set"
prepare_workspace(overwrite=False, preferred_language="en", workspace_dir=root)
print(f"Initialized: {root}")
PYTHON
```

### 3C. Install the plugin's dependencies (always required)

Installing or registering a plugin does **not** install its Python dependencies.
Install them into the environment that runs JiuwenSwarm:

```bash
uv pip install --python "$SWARM_DIR/.venv/bin/python" \
  'workswarm==0.2.7' \
  -r "$PLUGIN_REPO/plugins/observability-and-evaluation/requirements.txt"
uv pip check --python "$SWARM_DIR/.venv/bin/python"
```

If you don't have the repository, use the `requirements.txt` inside the plugin
ZIP instead. `uv pip check` should report no conflicts.

## 4. Get the plugin

Choose one:

| Option | For | Next step |
| --- | --- | --- |
| **A. Hub** | Users, once the plugin is published to the JiuwenSwarm Hub | In JiuwenSwarm's Extension Center, search for **Observability and Evaluation** and install it, then go to step 6 |
| **B. Release ZIP** | Users | Download `observability-and-evaluation-<version>.zip` from the project's releases, then go to step 5 |
| **C. Build from source** | Contributors, or to test unreleased changes | Follow 4C below |

### 4C. Build and validate the ZIP from source

```bash
"$SWARM_DIR/.venv/bin/python" "$PLUGIN_REPO/packaging/observability-and-evaluation/build_plugin.py" --check
"$SWARM_DIR/.venv/bin/python" "$PLUGIN_REPO/scripts/validate.py" --only observability-and-evaluation --require-hot-load
"$SWARM_DIR/.venv/bin/python" "$PLUGIN_REPO/packaging/observability-and-evaluation/build_plugin.py" --zip
```

| Command | Checks | Expected |
| --- | --- | --- |
| `build_plugin.py --check` | Package rules and static checks (L0/L1) | **PARTIAL** (hot loading is skipped here on purpose) |
| `validate.py --require-hot-load` | Also loads the plugin in this JiuwenSwarm environment (L2) | **PASS** |
| `build_plugin.py --zip` | Repeats L0/L1 and builds the ZIP | `dist/observability-and-evaluation-<version>.zip` |

## 5. Install the plugin

Point `PLUGIN_ZIP` at the ZIP from step 4B or 4C, then install it with
JiuwenSwarm's own package manager:

```bash
export PLUGIN_ZIP=/absolute/path/to/observability-and-evaluation-<version>.zip
# from a source build:  export PLUGIN_ZIP=$(ls "$PLUGIN_REPO"/dist/observability-and-evaluation-*.zip | sort -V | tail -1)

"$SWARM_DIR/.venv/bin/python" -I - <<'PYTHON'
import os
from pathlib import Path
from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.server.runtime.extension_package_manager import (
    import_plugin_package, install_plugin_package, show_plugin_package,
)

assert Path(get_user_workspace_dir()).resolve() == Path(os.environ["JIUWENSWARM_DATA_DIR"]).resolve()
existing = show_plugin_package("observability-and-evaluation")
if existing:
    raise SystemExit(f"Version {existing['version']} is already installed. "
                     "Follow 'Upgrade' in section 8 instead.")
package = import_plugin_package({"path": os.environ["PLUGIN_ZIP"]})   # copies it into local/
install_plugin_package(package)                                        # marks it installed
info = show_plugin_package(package["id"])
print(f"Installed {info['id']} {info['version']}: installed={info['installed']}")
PYTHON
```

Expected: `Installed observability-and-evaluation <version>: installed=True`. The
plugin is now in `$OJW_PLUGIN_DIR`.

If you see `Version … is already installed`, follow [Upgrade](#upgrade)
instead.

## 6. Evaluate a dialogue

Start JiuwenSwarm:

```bash
cd "$SWARM_DIR"
.venv/bin/jiuwenswarm-start
```

Open the printed frontend URL and make sure the agent has a model and
credentials configured. Then:

1. In a chat, select **Observability and Evaluation** in the plugin list.
2. Send a normal message and wait for the answer. This turn is recorded.
3. Send **`/evaluate`** as a separate message. Type it in full; it doesn't appear
   in the slash menu. The reply contains a link.
4. Open the **complete** link, including `#v=setup&token=…&context=…`. On a remote
   machine, forward both the frontend and evaluation ports first: see
   [Remote access](EVALUATION_REFERENCE.md#remote-access).
5. In **Setup**, check that your dialogue is selected. Enter the judge provider,
   model, endpoint and API key (independent from the agent's credentials), and
   press **Run evaluation**.

Good to know:

- `/evaluate` only returns the link: it makes no model call and is not itself
  recorded. The `open_dialogue_evaluation` tool returns the same link when an
  agent calls it.
- The evaluation service starts in the background on the first recorded turn,
  but analysis starts only when you press **Run evaluation**.
- Only recorded turns are evaluated. Turns whose recording is missing or
  unfinished are shown as incomplete or pending.
- In Setup you can choose **WorkSwarm dialogue** (the recorded dialogue) or
  **Upload from computer** (any trajectory JSON up to 10 MiB).

Results, strategies and taxonomies are explained in the
[Evaluation reference](EVALUATION_REFERENCE.md).

## 7. Where files are saved

Recordings are saved in the agent's workspace (which may differ from
`$JIUWENSWARM_DATA_DIR` if you configured another one), one folder per chat
session:

```text
<agent workspace>/
├── observability_traces/<session>/
│   ├── agent_<name>_ctx_1.json        # recording of exchange 1 (and _part_2, … if continued)
│   ├── dialogue.json                  # the whole dialogue, merged (system_architecture: shared_context): this is what gets evaluated
│   ├── metadata.json                  # session info, including service_root
│   └── evaluation/<UTC-timestamp>_ui/ # one folder per evaluation
└── evaluation_ui/                     # evaluation service state and history
```

Each evaluation keeps a snapshot of exactly what was evaluated, so later messages
never change an existing result. See
[Results and history](EVALUATION_REFERENCE.md#results-and-history) for the files
inside an evaluation folder.

To reopen a saved dialogue later without JiuwenSwarm's chat:

```bash
"$SWARM_DIR/.venv/bin/python" -I "$OJW_PLUGIN_DIR/rails/evaluation/open_dialogue.py" \
  /path/to/observability_traces/<session>/metadata.json
```

## 8. Upgrade or uninstall

Uninstalling removes only the plugin's own folder. Recorded dialogues and
evaluation results in the agent workspace are kept.

### Upgrade

1. Stop JiuwenSwarm: `"$SWARM_DIR/.venv/bin/jiuwenswarm-start" --stop default`
2. Stop the evaluation service, so the new code is used:
   ```bash
   "$SWARM_DIR/.venv/bin/python" -I "$OJW_PLUGIN_DIR/rails/evaluation/service_process.py" \
     --output-dir "<agent workspace>/evaluation_ui" --stop
   ```
3. Update the dependencies for the new version (step 3C, with the new
   `requirements.txt`).
4. Uninstall the old version (below), then install the new ZIP (step 5).
5. Start JiuwenSwarm again: `"$SWARM_DIR/.venv/bin/jiuwenswarm-start"`

### Uninstall

With JiuwenSwarm stopped:

```bash
"$SWARM_DIR/.venv/bin/python" -I - <<'PYTHON'
from jiuwenswarm.server.runtime.extension_package_manager import uninstall_plugin_package
uninstall_plugin_package({"id": "observability-and-evaluation"})
print("Uninstalled observability-and-evaluation")
PYTHON
```

To restart JiuwenSwarm without changing plugins:
`"$SWARM_DIR/.venv/bin/jiuwenswarm-start" --restart default`.

## 9. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `validate.py --require-hot-load` fails | Plugin dependencies missing from JiuwenSwarm's environment | Repeat step 3C with `$SWARM_DIR/.venv/bin/python` |
| Plugin not in the chat's plugin list | Not installed, or JiuwenSwarm wasn't restarted | Check step 5's output; restart JiuwenSwarm |
| `Version … is already installed` (or `plugin package already exists in local`) | A version is already installed | Follow [Upgrade](#upgrade) |
| `/evaluate` is answered like a normal message | Plugin not selected for this chat | Select **Observability and Evaluation** for the chat, send a normal message, then `/evaluate` |
| Setup shows no dialogue | No turn recorded yet, or a different agent workspace | Send a normal message first; check the workspace path in `metadata.json` |
| Link doesn't open on a remote machine | Ports not forwarded | Forward both the frontend port (e.g. 5173) and the evaluation port (e.g. 8765) |
| UI behaves like the old version after an upgrade | The evaluation service still runs the old code | Stop it (Upgrade step 2) and open the link again |

Judge errors such as `ValueError` or `APIConnectionError` are covered in the
[shared troubleshooting table](EVALUATION_REFERENCE.md#troubleshooting).
