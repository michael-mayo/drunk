# drunk

Realistic terrain for **Cities: Skylines II**, made by simulating drunks: random walkers that
stagger around their homes and leave a trail of small bumps. A map is built up gang by gang, and
a gang of drunks is kept only if it makes the map look more like real terrain.

| Seed 41 | Seed 42 | Seed 43 |
|---|---|---|
| ![Map, seed 41](sample_images/map_seed41.png) | ![Map, seed 42](sample_images/map_seed42.png) | ![Map, seed 43](sample_images/map_seed43.png) |

Each image is a whole CS2 world map (57.3 km across). The map wraps around: the left edge
continues into the right and the top into the bottom.

## Install

```bash
conda create -n drunk python=3.14
conda activate drunk
pip install -r requirements.txt
```

You also need Git LFS to get the images in `sample_images/`.

## Make a map

```bash
python -m app.ui
```

This opens the app at <http://localhost:9000/> (under WSL, in your Windows browser). If no
browser opens, go to that address yourself. Stop the app with Ctrl+C.

1. **Build.** Enter a **Seed** (or press 🎲) and press **Build map**. A build takes about
   1.5 minutes. The first one takes about 10 s longer while the simulation compiles. The map
   appears after the first gang and grows as more gangs are kept.
2. **Adjust the view.** These change instantly and never alter the map's shape:
   - **Sea**: the share of the map under water.
   - **Relief**: metres from the lowest to the highest point. Lower is gentler, higher is more
     mountainous.
   - **Editor sea level**: the sea level set in the CS2 editor (default 511.7 m). Heights are
     shifted so the coastline lands exactly on it.

   The lowest, coast and highest heights are shown underneath. A warning appears if anything
   is flattened at 0 m or 4096 m, the limits of a CS2 heightmap.
3. **Place the playable area.** The white square is the 14.3 km playable area. Click anywhere
   on the map to move it there.
4. **Export.** **World map** and **Playable area** each download a 4096 × 4096 16-bit PNG. Import
   them in the CS2 map editor as the world map and the heightmap. The editor looks for them in
   `%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\Heightmaps`. Each file
   name records its seed, sea share and position, and the settings are also stored inside the
   PNG.

**Same seed, same map.** A seed and a number of **Gangs to try** always give the same map.
Trying fewer gangs gives an earlier stage of the same map.

The **Realism** panel shows how close the map is to real terrain (see below) as it builds.

### Without the UI

```bash
python -m app.build_map 41 42 43
```

This builds a map for each seed, prints its progress and statistics, and saves previews as
`output/map_seed<seed>.png`.

## How it works

**Drunks.** A drunk takes 1000 steps from its home in random directions. It is pulled back home
more strongly the further it strays. After every step it drops a small, randomly stretched
Gaussian bump, each a little lower than the last. A *gang* is a group of drunks with the same
step length. Long steps make broad hills and ranges, short steps make fine texture. The
simulation runs in parallel compiled code ([numba](https://numba.pydata.org/)).

**Realism score.** Four statistics describe how terrain varies:

- **beta**: how fast detail fades from large to small features (spectral slope)
- **H**: how quickly height differences grow with distance (roughness)
- **HI**: mean height as a fraction of the range (hypsometric integral)
- **skew**: skewness of the heights: are there more lowlands or more highlands?

These are measured on the whole world and on its 16 playable-sized squares, and compared with
real terrain of the same sizes. The real values come from Copernicus 30 m elevation data of the
Appalachians, Rockies, Pyrenees, Alps, Hesse uplands and Scottish Highlands. The score is how many
standard deviations the map is from real terrain, averaged (RMS) over the statistics: **0 is typical real
terrain, and under 1 is within normal variation.** The sample maps score 0.22–0.33.

**Building.** Each trial draws a random gang: its step length (75 m to 2.4 km), number of
drunks, homeward pull, and how strongly its drunks settle on existing high ground. The gang is
tried at five strengths. It is kept at the best one if that improves the score; otherwise it is
dropped. 60 trials typically keep about 20 gangs.

## Settings

Settings are constants at the top of each module:

| File | Setting | Default |
|---|---|---|
| `app/build_map.py` | `TRIALS`: gangs tried per map | 60 |
| | `STEP_KM`, `DRUNKS_PER_STEP_AREA`, `KAPPA_MAX`, `AFFINITIES`, `RELATIVE_WEIGHTS`: ranges the random gangs are drawn from | |
| `app/map.py` | `SEA_FRACTION`, `VERTICAL_M`, `EDITOR_SEA_LEVEL_M`: the view's starting values | 0.4, 2500 m, 511.7 m |
| | `GRID`: cells per side of the map (56 m cells) | 1024 |
| `app/drunk.py` | `STEPS`, `DECAY`: steps per drunk and how fast its bumps shrink | 1000, 0.999 |
| `app/ui.py` | `HOST`, `PORT`: where the app is served | 127.0.0.1, 9000 |

## Project layout

```
app/drunk.py       drunks and gangs: the walk simulation
app/map.py         the map: heights, preview image, CS2 heightmap export
app/build_map.py   realism score and the greedy builder (also a command-line tool)
app/ui.py          the web app (entry point)
tests/             tests: run `pytest` (about 10 s)
sample_images/     images for this README
output/            generated files (not in git)
```
