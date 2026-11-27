# AIND Experiment: Aind.Behavior.VrForaging + Physiology

> [!WARNING]
> This repository is an **internal deployment** for the Allen Institute. It wires together
> specific rig configurations, network paths, and Allen-Institute-only
> services (Dataverse, AIND watchdog/data-transfer, waterlog). It is unlikely to be directly
> useful outside of the building without significant adaptation.

A repository for an experiment that combines VrForaging with a physiology modality.

The plan is to keep adding physiology submodules (e.g. ephys, other photometry/imaging rigs)
combined with VrForaging the same way FIP is. **Every physiology submodule pinned here is assumed
to be compatible with the single pinned VrForaging version** — one known-good combination, not a
matrix of versions. If you find a pinned combination that's actually incompatible, please open an
issue.

## Architecture

This repository is a thin composition layer, not a standalone codebase. It stitches together
independent Bonsai/Python projects (one behavior repo, one-or-more physiology repos), plus a small
amount of glue code:

- **`Aind.Behavior.VrForaging/`**, **`Aind.Physiology.Fip/`**, and
  **`Aind.Behavior.Device.Olfactometer/`** — git submodules pointing at pinned revisions of the
  [VrForaging](https://github.com/AllenNeuralDynamics/Aind.Behavior.VrForaging),
  [FIP](https://github.com/AllenNeuralDynamics/Aind.Physiology.Fip), and
  [Olfactometer](https://github.com/AllenNeuralDynamics/Aind.Behavior.Device.Olfactometer) repositories.
  Each submodule provides its own Bonsai workflow (`src/main.bonsai`),
  rig/task-logic schemas, and data mappers, and is developed, tested, and released independently
  of this repository.
- **`experiments/`** — the workflows and their helpers:
  - `vr_foraging.py` — the VrForaging workflow and its `calibration`. One workflow covers every combination of
    modalities: its `modalities` step decides whether FIP records, and the FIP steps only run when it does
    (`when=`). `vr-foraging-fip` is a variant with that step fixed, until it is decided per session.
  - `recover_session.py` — loads a session from its directory, then reuses the VrForaging steps that run after the
    session.
  - `fip.py` — FIP's rig library, app, mapper and QC, and the FIP-only workflows.
  - `olfactometer.py` — olfactometer calibration and its recovery.
  - `common.py` (logs, data transfer), `vr_foraging_helpers.py`, `curriculum_helpers.py`.
- **`main.py`** — the workflow registry. It imports the clabe `Workflow`s so
  [clabe](https://github.com/AllenNeuralDynamics/clabe) can discover them. Each workflow is a list of steps
  (session, rig and task configuration, review, preflight checks, register, acquire, mappers, data transfer) that
  clabe runs and serves to the terminal, the browser or an agent. Currently defined workflows:
  - `vr-foraging` — run VrForaging on its own.
  - `vr-foraging-fip` — run VrForaging and FIP concurrently as a single combined session.
  - `fip` — run FIP acquisition on its own, without the behavior component.
  - `fip-photobleaching` — run the FIP photobleaching workflow (blue light on, no data recorded).
  - `calibration` — run only the VrForaging rig for calibration; nothing is recorded.
  - `calibrate-olfactometer` — calibrate olfactometer hardware using its local rig configuration.
  - `recover-session` — reprocess (curriculum/mappers/QC/transfer) a session whose Bonsai
    workflow(s) already completed, e.g. after a launcher crash.
  - `recover-olfactometer-calibration` — the same for an olfactometer calibration.
- **`clabe`** (`aind-clabe`, installed as a dependency) is the framework that provides the
  `Workflow` runner, stores (rig/task/trainer-state selection), data-transfer services, curriculum
  runner, Smartsheet schedule lookups for scientific contacts and transfer metadata, and the generic CLI used to
  run `main.py`. The workflows need clabe 0.18 (currently the `0.18.0rc7` pre-release).
- **`experiments/_compat.py`** — aliases the clabe module the pinned submodules still import by its old name
  (`clabe.data_mapper.aind_data_schema`). Remove it once the submodules are updated.
- **`pyproject.toml`** / **`uv.lock`** define the single Python environment (managed by
  [`uv`](https://docs.astral.sh/uv/)) that all of the above run in. `[tool.ruff]` excludes the
  submodules from linting — they are linted by their own CI.

## Getting started

1. Ensure the requirements of both repositories [Aind.Behavior.VrForaging](https://github.com/AllenNeuralDynamics/Aind.Behavior.VrForaging) and [Aind.Physiology.Fip](https://github.com/AllenNeuralDynamics/Aind.Physiology.Fip/) are fulfilled. This repository requires uv to be installed!
2. Clone this repository
3. Run `./scripts/deploy.cmd`
4. Ensure `clabe.yml` is defined in your system. There is an example in `examples/clabe.yml` that can be used to get you going by copying it into `./local`.

## Launching an experiment

`main.py` defines the available experiments (see [Architecture](#architecture)). Launch one with
the generic `clabe` CLI:

```powershell
uv run clabe run                                      # prompts for the workflow to run
uv run clabe run --experiment-name vr-foraging-fip    # or name it
```

The run is also served at <http://localhost:8090>, to follow or answer from a browser. You can also pass any `clabe`
flags (e.g. `--frontend`, `--debug-mode`, `--no-serve`). Run `uv run clabe run --help` to see all available options,
and `uv run clabe list` to list the workflows and their steps.

## Testing changes

The submodules have their own test suites and CI. This repository has unit tests for its helpers and the
integration tests that merge the metadata of the modalities (these need Bonsai deployed: `./scripts/deploy.cmd`):

```powershell
uv run pytest
uv run clabe check experiments --strict   # checks the workflows' wiring and annotations without running anything
```

To validate a change here (e.g. bumping a submodule to a new release, or editing `main.py`/`experiments/`):

1. Open a new branch off `main`.
2. If you need to test against a new submodule release, update the pointer(s):
   ```powershell
   cd Aind.Behavior.VrForaging   # or Aind.Physiology.Fip, or any other physiology submodule
   git fetch --tags
   git checkout <tag>
   cd ..
   ```
   (Note: a scheduled workflow, `.github/workflows/submodule-update.yml`, already opens a PR
   automatically whenever a submodule has a newer GitHub release. It updates each submodule
   independently — it does **not** verify that the resulting VrForaging + physiology combination
   is actually compatible, so treat its PRs as a starting point, not a guarantee.)
3. Run `uv sync` to pick up any dependency changes.
4. Manually run the affected experiment(s) with `uv run clabe run` on a rig (or with a
   local/dev configuration) to confirm the VrForaging + physiology combination behaves correctly
   — there is no substitute for an end-to-end run here.
5. Open a pull request back to `main`. CI (`.github/workflows/lint.yml`) will run `ruff format
   --check`, `ruff check` and `clabe check experiments --strict` against the root-level code (`main.py`,
   `experiments/`); it does not lint the submodules.
