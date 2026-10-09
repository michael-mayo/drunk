# drunk

Realistic terrain for **Cities: Skylines II**, made by simulating drunks: random walkers that
stagger around their homes and leave a trail of small bumps. A map is built up gang by gang, with
the low ground now and then buried under flat alluvial plains, and each step is kept only if it
makes the map look more like real terrain.

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

1. **Build.** Enter a **Seed** (or press 🎲), set the **Sea** slider to the share of the map you
   want under water, and press **Build map**. The sea share stays fixed, and the builder shapes
   the land to fit it: a wet map is steered towards real coastal places, a dry one towards
   inland ones. Tick **Auto sea** to let the builder choose the share instead; it picks the one
   that fits real terrain best, and may change it as the map grows. A build takes about
   4–5 minutes. The first one takes about 10 s longer while the simulation compiles. The map
   appears after the first gang and grows as more steps are kept. While it builds, the builder
   picks the best city site and a relief to match the most similar real places, and the view
   follows them.
2. **Adjust the view.** These change instantly and never alter the map's shape:
   - **Sea**: the share of the map under water (locked while a build runs). The map was
     built for the share it was built with, but you can still try others.
   - **Relief**: metres from the lowest to the highest point. Lower is gentler, higher is more
     mountainous. A build sets it so the land's relief matches the median of the real squares the
     map most resembles; the statistics ignore height, so without this a map shaped like Copenhagen
     could come out with Alpine relief.
   - **Editor sea level**: the sea level set in the CS2 editor (default 511.7 m). Heights are
     shifted so the coastline lands exactly on it.

   The lowest, coast and highest heights are shown underneath. A warning appears if anything
   is flattened at 0 m or 4096 m, the limits of a CS2 heightmap.
3. **Place the playable area.** The white square is the 14.3 km playable area. It starts on
   the most city-like site. Click anywhere on the map to move it there.
4. **Export.** **World map** and **Playable area** each download a 4096 × 4096 16-bit PNG. Import
   them in the CS2 map editor as the world map and the heightmap. The editor looks for them in
   `%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\Heightmaps`. Each file
   name records its seed, sea share and position, and the settings are also stored inside the
   PNG.

**Same seed, same map.** A seed, a number of **Trials** and a sea share (or Auto sea)
always give the same map. Fewer trials give an earlier stage of the same map.

The **Realism** panel shows how close the map is to real terrain (see below) as it builds.

### Without the UI

```bash
python -m app.build_map 41 42 43 --sea 30
```

This builds a map for each seed with 30% sea (leave out `--sea` for auto), prints its progress
and statistics, and saves previews as `output/map_seed<seed>.png`.

## How it works

**Drunks.** A drunk takes 1000 steps from its home in random directions. It is pulled back home
more strongly the further it strays. After every step it drops a small, randomly stretched
Gaussian bump, each a little lower than the last. A *gang* is a group of drunks with the same
step length. Long steps make broad hills and ranges, short steps make fine texture. The
simulation runs in parallel compiled code ([numba](https://numba.pydata.org/)).

**Plains.** Drunks only add bumps, so on their own they leave the lowlands as rough as the
mountains. Real coasts and basins are the opposite: valleys between ranges fill with sediment into
flats, and the ranges rise sharply out of them (in Vancouver, Busan or Hamilton, a fifth or more of
the land lies in the lowest 2% of the relief and is almost dead flat). A *plains* step finds the land
whose height, smoothed over about a kilometre, is below a mountain front, and replaces it with a
surface rising gently from the coast, keeping only a little of its small relief. It blends into the
untouched ranges over a narrow band, giving a sharp mountain foot. The sea and the sea share are
unchanged.

**Realism score.** Nine statistics describe a landscape, with water left out of all but one:

- **H fine** and **H coarse**: how quickly height differences grow with distance, over short
  (1/64–1/16 of the square's side) and long (1/16–1/4) distances
- **HI**: mean land height as a fraction of its range (hypsometric integral)
- **skew**: skewness of land heights: more lowlands or more highlands?
- **water**: the share of the map that is water
- **hollows**: the share of land in closed hollows, which could only drain by filling up
  (real land: about 2%)
- **concavity**: how channel slope falls as the drained area grows: valleys steep at the head
  and gentle near the mouth
- **tau**: how drained area is shared out among cells, a signature of branching river networks
- **contrast**: how much steeper the high land (above 40% of the relief) is than the low land
  (below 5%), as a power of ten: real squares are typically 5 times steeper (0.7), and coasts
  backed by mountains 30–80 times (1.5–1.9)

They are compared with real terrain from [FABDEM](https://doi.org/10.5523/bris.s5hqmjcdj8yo2ibzi9b4ew3sn),
30 m elevation data with buildings and trees removed. The reference has 61 squares the size of
a world map: 31 centred on cities (Vancouver, Rio, Genoa, Lyon, Denver, Kathmandu, Hamilton and
others) and 30 on wild terrain (mountains, uplands, quiet coasts and plains). The score combines
two distances:

- the whole map against all 61 squares;
- its most city-like playable-sized square against the 14.3 km around each of the 31 cities,
  so every map has a realistic place to build a city.

Each distance is to the three most similar real squares, not to the average of all of them:
the average of the Alps and Kansas is terrain that exists nowhere, while the nearest squares
ask for a map that looks like *some* real place. The distance to a square is how many standard
deviations of real terrain the map's statistics differ by (RMS over the nine), and the score is
the RMS of the two distances: **0 matches real squares exactly; a real square scores about 0.6
against the others (0.75 for the 90th percentile).** The panel shows which real places the map is
closest to. The sea share is the one you set, or, with Auto sea, the one that fits best.

How do we know the score measures realism? Each real square, scored against the other 59,
should score low, and fakes high. Maps built by the previous score (the RMS z-score against the
average) beat 13–15% of real squares under it, though they visibly lack connected valleys; under
the nearest-squares distance they beat only 3–5%: they have about three times too much land in
hollows and too little valley concavity, because drunks pile up hills without carving valleys.
The sample maps above (30% sea) score 0.75–0.85, beating 7–10% of real squares (the old samples, under the old score, beat
3–5%). Seed 43 has kept coastal plains under steep hills, and its best city site most resembles
Vancouver, Naples and San Francisco. Seeds 41 and 42 resemble flat lowlands such as Copenhagen and
Buenos Aires, and they are still rougher than real lowlands: gangs added after a plains step put
texture back on it.

**Building.** Each trial draws a random gang: its step length (75 m to 2.4 km), number of
drunks, homeward pull, and how strongly its drunks settle on existing high ground. The gang is
tried at five strengths. It is kept at the best one if that improves the score; otherwise it is
dropped. Once the map has something on it, a quarter of the trials instead draw random plains
(mountain front height, plain height and rise, how much small relief survives), kept only if
they improve the score.

## Reference data

The 61 reference squares are in `fabdem/data/windows/` (git LFS, about 100 MB), with a contact sheet
in `fabdem/data/sites.png` and every square's statistics in `fabdem/data/reference.npz` (read by
the builder) and `fabdem/data/reference.txt` (readable). To
change the reference, edit the sites in `fabdem/locations.py`, then run:

```bash
python fabdem/download.py    # fetch the FABDEM tiles for sites not yet cut (about 25 MB per tile, not kept in git)
python fabdem/reference.py   # cut and measure the squares, and save their statistics and relief
```

Squares already cut are reused (and their tiles not downloaded), so the raw tiles are only needed
for new sites. To re-cut a site whose coordinates you changed, delete its square first. FABDEM V1-2 (Hawker et al., 2022) is licensed
CC BY-NC-SA 4.0, for non-commercial use only.

## Settings

Settings are constants at the top of each module:

| File | Setting | Default |
|---|---|---|
| `app/build_map.py` | `TRIALS`: gangs and plains tried per map | 60 |
| | `NEIGHBOURS`: real squares a map is compared with | 3 |
| | `SEA_FRACTIONS`: sea shares the builder chooses from with Auto sea | 0–60% |
| | `STEP_KM`, `DRUNKS_PER_STEP_AREA`, `KAPPA_MAX`, `AFFINITIES`, `RELATIVE_WEIGHTS`: ranges the random gangs are drawn from | |
| | `PLAINS_CHANCE`: share of trials that propose plains | 0.25 |
| | `FRONT`, `PLAIN_SHARE`, `LENGTH_KM`, `KEEP`, `SMOOTH_KM`: ranges random plains are drawn from | |
| | `CONTRAST_LOW`, `CONTRAST_HIGH`: the low and high land compared by contrast | 5%, 40% of relief |
| `app/map.py` | `VERTICAL_M`, `EDITOR_SEA_LEVEL_M`: the view's starting relief and editor sea level | 2500 m, 511.7 m |
| | `GRID`: cells per side of the map (56 m cells) | 1024 |
| `app/drunk.py` | `STEPS`, `DECAY`: steps per drunk and how fast its bumps shrink | 1000, 0.999 |
| `app/ui.py` | `HOST`, `PORT`: where the app is served | 127.0.0.1, 9000 |

## Project layout

```
app/drunk.py       drunks and gangs: the walk simulation
app/plains.py      alluvial plains: flattening low ground below a mountain front
app/map.py         the map: heights, preview image, CS2 heightmap export
app/build_map.py   realism score and the greedy builder (also a command-line tool)
app/ui.py          the web app (entry point)
fabdem/            real-terrain reference: site list, download and measuring scripts, data/
tests/             tests: run `pytest` (about 50 s)
sample_images/     images for this README
output/            generated files (not in git)
```
