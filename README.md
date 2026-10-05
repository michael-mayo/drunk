# drunk

## Overview

A simple 2D random-walk ("drunkard's walk") simulator. Each `Drunk` starts at the origin and, on
every step, staggers a fixed distance in a random direction that is biased back towards home, and
deposits a randomly oriented 2D Gaussian at its new location. A `CompositeDrunk` groups several
independent drunks, runs them in parallel worker processes, and renders the sum of all their
deposits. The program builds composites of configurable sizes (3 and 7 by default), prints a summary
of each, and saves a heatmap of each composite's combined deposits (with every member's path overlaid
faintly) as a PNG.

### Sample output

| Composite of 3 drunks | Composite of 7 drunks |
|---|---|
| ![Composite of 3](sample_images/composite_3_drunks.png) | ![Composite of 7](sample_images/composite_7_drunks.png) |

Sample images are kept in [`sample_images/`](sample_images/) (generated with the `config.yaml`
values listed under [Configuration](#configuration)).

### Homeward bias

Step directions are drawn from a [von Mises distribution](https://en.wikipedia.org/wiki/Von_Mises_distribution)
(the circular analogue of a normal distribution) centred on the bearing back to the origin,
`atan2(-y, -x)`. Its concentration `κ` depends only on the drunk's distance `r` from the origin:

```
κ(r) = kappa_max · (1 − exp(−r / r0))
```

`kappa_max` and `r0` are constants from `config.yaml`. At the origin `κ = 0`, so directions are
uniform. As `r` grows, `κ` rises smoothly towards `kappa_max` and steps increasingly point home.
For example, with `kappa_max = 2`, `r0 = 10`:

| Drunk at | r | κ | P(step has homeward component) | P(within ±45° of home) |
|---|---|---|---|---|
| (0, 0) | 0 | 0 | 50% (uniform) | 25% (uniform) |
| (0, 1) | 1 | 0.19 | 56% | 29% |
| (3, 4) | 5 | 0.79 | 73% | 44% |
| (10, 10) | 14.1 | 1.51 | 87% | 59% |

Setting `kappa_max: 0` recovers a plain uniform random walk.

### Gaussian deposits

After each step the drunk deposits an unnormalised 2D Gaussian (`app/gaussian.py`,
`GaussianDeposit`) centred on its new location:

- **Orientation:** the principal axes are rotated by an angle drawn uniformly from [0, π).
- **Shape:** variance `variance` along the major axis and `variance × u` along the minor axis,
  with `u` drawn uniformly from (0, 1], so each blob has a random elongation.
- **Amplitude:** the peak height (value at the centre). The k-th deposit (k = 0, 1, …) has
  amplitude `initial_amplitude × decay^k` (1.0, 0.999, 0.998, …), so later deposits are weaker.
  After 1000 steps the amplitude is ≈ 0.37.

The drunk stores these deposits rather than bare locations (its path is the sequence of deposit
centres, starting at (0, 0)). `to_png` evaluates their sum on a grid covering the path plus a
3-standard-deviation margin, and renders it as a heatmap.

**Cutoff.** A Gaussian falls to a fraction `c` of its peak at `m = sqrt(2·ln(1/c))` standard
deviations from its centre. With `plot.cutoff = 1/4096`, `m ≈ 4.08`, so with variance 1 each
deposit is negligible beyond about 4 units. `GaussianDeposit.add_to_grid` therefore evaluates each
Gaussian only on the grid points inside the bounding box of that cutoff ellipse and adds the
result into that patch of the image. With variance 1 and a 400×400 grid, that patch is about
4% of the grid. Values skipped are below `cutoff × amplitude` (at most 1/4096 per deposit). For a
7 × 1000-step composite this renders about 40× faster than evaluating every Gaussian over the
whole grid, with a maximum error of about 6×10⁻⁵ of the image's peak. `cutoff: 0` gives the exact,
slow evaluation.

### Composite drunks and parallelism

`CompositeDrunk` (`app/composite_drunk.py`) inherits from `Drunk` and is built from a list of seeds,
creating one member `Drunk` per seed with shared parameters. It has no randomness or deposits of
its own:

- `location` is the centroid of the members' locations; `positions` is the centroid path.
- `step()` advances every member by one step, sequentially (a single step is far cheaper than
  sending a drunk to another process).
- `steps(n)` advances every member by `n` steps **in parallel**, one member per worker process.
- `to_png()` evaluates each member's deposit field on a shared grid **in parallel**, sums the
  fields, and overlays every member's path.

Processes are used rather than threads because this Python build has the GIL enabled, so threads
cannot run the pure-Python stepping loop concurrently. The design is race-free by construction:
each worker receives its own pickled copy of one member (each member has its own RNG) and returns a
result, so no mutable state is shared. `ProcessPoolExecutor.map` returns results in submission
order, and they are combined only in the parent process, so output is bit-for-bit identical to a
sequential run.

## Requirements

- Python 3.14
- Conda environment: `drunk`
- Git LFS (images and other binaries are tracked via LFS — see `.gitattributes`)
- Pinned pip libraries: see [`requirements.txt`](requirements.txt)

## Setup

```bash
conda create -n drunk python=3.14
conda activate drunk
pip install -r requirements.txt
git lfs install
git lfs pull
```

## How to run

From the project root:

```bash
conda activate drunk
python app/main.py      # or equivalently: python -m app.main
```

Example console output (abridged):

```
CompositeDrunk(n=3, step_size=1.0, kappa_max=0.25, r0=10.0, variance=1.0, decay=0.999, steps=1000, centroid=(-3.28, -5.72), centroid_distance=6.60)
  Drunk(seed=383329928, step_size=1.0, kappa_max=0.25, r0=10.0, variance=1.0, decay=0.999, steps=1000, location=(2.57, -5.70), distance=6.25, last_amplitude=0.368)
  Drunk(seed=3324115917, ...)
  Drunk(seed=2811363265, ...)
Saved /home/michael/drunk/output/composite_3_drunks.png
CompositeDrunk(n=7, ..., centroid=(-1.41, -0.77), centroid_distance=1.60)
  ...
Saved /home/michael/drunk/output/composite_7_drunks.png
```

One image per composite is written to the configured output folder (default `output/`) as
`composite_<n>_drunks.png`.

## Configuration

All key parameters and settings are read from [`config.yaml`](config.yaml) at the project root by
`app/config.py` (`load_config()`), which returns a typed, read-only `Config` object. Every setting
is required, so a missing key raises an error at startup. Relative paths are resolved against the
folder containing `config.yaml`.

| Setting | Type | Current value | Description |
|---|---|---|---|
| `seed` | int | `42` | Master seed for the RNG that generates every member drunk's seed |
| `composite.sizes` | list of int | `[3, 7]` | One `CompositeDrunk` is built per entry, with that many member drunks |
| `parallel.max_workers` | int or `null` | `null` | Maximum worker processes per composite (`null` = one per CPU, capped at the number of members) |
| `walk.num_steps` | int | `1000` | Number of steps each member drunk takes |
| `walk.step_size` | float | `1.0` | Distance moved on each step |
| `walk.kappa_max` | float | `0.25` | Maximum homeward bias, reached far from the origin (`0` = uniform walk) |
| `walk.r0` | float | `10.0` | Distance over which the bias builds up (`κ` ≈ 63% of `kappa_max` at `r = r0`) |
| `deposit.variance` | float | `1.0` | Major-axis variance of each Gaussian (minor axis = `variance × u`, `u` ~ U(0, 1]) |
| `deposit.initial_amplitude` | float | `1.0` | Peak amplitude of the first deposit |
| `deposit.decay` | float | `0.999` | Geometric factor: the k-th deposit has amplitude `initial_amplitude × decay^k` |
| `plot.grid_points` | int | `400` | Points per side of the grid used to render the summed deposits |
| `plot.cutoff` | float | `0.000244140625` (1/4096) | Each Gaussian is evaluated only where it exceeds `cutoff` × its peak (≈ 4.08 sd); `0` = exact |
| `paths.sample_images_dir` | path | `sample_images` | Folder of sample images shown in this README |
| `paths.output_dir` | path | `output` | Folder where generated PNGs are written (git-ignored) |

## Programs and command-line parameters

### `app.main`

Seeds a master RNG from `config.yaml`. For each size in `composite.sizes` it generates that many
member seeds, builds a `CompositeDrunk`, runs it for `walk.num_steps` steps (members in parallel),
prints its summary, and saves a heatmap of its combined deposits to
`<output_dir>/composite_<n>_drunks.png`.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `--config` | path | optional | `config.yaml` (project root) | YAML config file to load |

Examples:

```bash
python app/main.py                          # use config.yaml at the project root
python app/main.py --config my_config.yaml  # use an alternative config file
python -m app.main                          # same, run as a module
```

### `app.drunk.Drunk` (library class)

| Member | Description |
|---|---|
| `Drunk(seed, step_size=1.0, kappa_max=2.0, r0=10.0, variance=1.0, decay=0.999, initial_amplitude=1.0)` | Create a drunk at (0, 0) with its own RNG seeded from `seed` |
| `step()` | Move `step_size` in a von Mises direction biased towards the origin, deposit a Gaussian there; returns the new location |
| `kappa()` | Current bias strength `κ(r)` at the drunk's location |
| `steps(n=100)` | Call `step()` `n` times; returns the final location |
| `location` | Current `(x, y)` |
| `deposits` | List of `GaussianDeposit`s, one per step |
| `positions` | List of every `(x, y)` visited, starting at `(0, 0)` (derived from the deposit centres) |
| `density(gx, gy, cutoff=1/4096)` | Sum of all deposits on the regular grid with 1D axes `gx`, `gy`; returns shape `(len(gy), len(gx))` |
| `num_steps` | Steps taken so far |
| `distance_from_origin()` | Straight-line distance from (0, 0) |
| `to_png(filename, grid_points=400, cutoff=1/4096)` | Save a heatmap of the summed deposits (path, start and end overlaid) to an image file |
| `str(drunk)` | One-line summary: settings, steps, location, distance, last deposit amplitude |

### `app.composite_drunk.CompositeDrunk(Drunk)` (library class)

| Member | Description |
|---|---|
| `CompositeDrunk(seeds, step_size=1.0, kappa_max=2.0, r0=10.0, variance=1.0, decay=0.999, initial_amplitude=1.0, max_workers=None)` | Create one member `Drunk` per seed, sharing the given parameters |
| `drunks` | The member `Drunk`s |
| `step()` | Advance every member by one step (sequential); returns the centroid |
| `steps(n=100)` | Advance every member by `n` steps in parallel processes; returns the centroid |
| `location` / `positions` | Centroid of the members' current location / path |
| `density(gx, gy, cutoff=1/4096)` | Sum of all members' deposits on the grid `gx`, `gy` (sequential) |
| `to_png(filename, grid_points=400, cutoff=1/4096)` | Evaluate members' fields in parallel, sum them, and save a heatmap with every member's path |
| `str(composite)` | Summary line for the composite followed by one line per member |

## System diagram

```mermaid
classDiagram
    direction LR
    namespace app {
        class main {
            +main() None
        }
        class Drunk {
            +seed int
            +step_size float
            +kappa_max float
            +r0 float
            +deposits list~GaussianDeposit~
            +positions list
            +kappa() float
            +density(gx, gy, cutoff) ndarray
            +step() tuple
            +steps(n) tuple
            +to_png(filename) None
        }
        class CompositeDrunk {
            +seeds list~int~
            +drunks list~Drunk~
            +max_workers int
            +step() tuple
            +steps(n) tuple
            +to_png(filename) None
        }
        class GaussianDeposit {
            +center tuple
            +angle float
            +var_major float
            +var_minor float
            +amplitude float
            +evaluate(xs, ys) ndarray
            +add_to_grid(field, gx, gy, cutoff) None
        }
        class rendering {
            +square_grid(points, margin, grid_points)
            +save_heatmap(filename, field, gx, gy, paths, title)
        }
        class load_config {
            +load_config(path) Config
        }
        class Config {
            +seed int
            +composite CompositeConfig
            +parallel ParallelConfig
            +walk WalkConfig
            +deposit DepositConfig
            +plot PlotConfig
            +paths PathsConfig
        }
    }
    class config_yaml["config.yaml"]
    class ProcessPool["ProcessPoolExecutor workers"]
    class PNG["output/composite_n_drunks.png"]
    main --> load_config : calls
    load_config ..> config_yaml : reads
    load_config ..> Config : creates
    main ..> CompositeDrunk : creates & runs
    Drunk <|-- CompositeDrunk : inherits
    CompositeDrunk *-- Drunk : n members
    CompositeDrunk ..> ProcessPool : steps() / to_png() fan out members
    Drunk *-- GaussianDeposit : deposits each step
    Drunk ..> rendering : to_png uses
    CompositeDrunk ..> rendering : to_png uses
    rendering ..> PNG : writes
```

```mermaid
flowchart LR
    P[parent: CompositeDrunk.steps] -->|pickled copy of member i| W1[worker 1: member.steps n]
    P --> W2[worker 2: member.steps n]
    P --> Wn[worker n: member.steps n]
    W1 -->|advanced member| M[parent: map results in order, replace members]
    W2 --> M
    Wn --> M
```

## Project layout

```
drunk/
├── app/                   # Main application package
│   ├── config.py          # Loads config.yaml into typed dataclasses
│   ├── drunk.py           # Drunk random-walker class
│   ├── composite_drunk.py # CompositeDrunk: n drunks run in parallel processes
│   ├── gaussian.py        # GaussianDeposit: oriented anisotropic 2D Gaussian
│   ├── rendering.py       # Shared grid and heatmap PNG rendering
│   └── main.py            # Entry point: runs the composites and saves their heatmaps
├── sample_images/         # Sample output images shown in this README
├── output/                # Generated images (git-ignored)
├── config.yaml            # All key parameters and settings
├── requirements.txt       # Pinned pip dependencies
├── README.md
├── .gitignore
└── .gitattributes         # Line-ending normalisation and Git LFS tracking
```
