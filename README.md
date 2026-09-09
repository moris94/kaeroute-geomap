# Kaeroute offline maps

Public geographic build pipeline for the 帰宅ルート iOS app. No application source or user data is published here.

Geofabrik dated OSM PBF → bounded osmium extracts → Planetiler 0.10.2 → grid PMTiles → GitHub Releases → manifest.json.

Run **Build offline maps** in Actions with scope `poc` first, then `full` with a new immutable version (for example `2026-09-v1`). Defaults: approximately 15 km geographic grid, z8–16, 1 km route buffer. All grid geometry is published in the manifest. Operations use geographic batch IDs, not prefectural boundaries. Each batch has at most 36 PMTiles assets, below the 900-file release target. Inputs use the dated 2026-09-01 Geofabrik extracts; checksums are recorded and verified. Never replace published assets: choose a new version when data, tools, grid or style changes.

The planning job derives occupied cells from all OSM nodes, adds a full neighboring cell as margin, then performs complete-way/multipolygon extracts in bounded batches. Coast and border geometry comes from the same country input. PMTiles retain full intersecting vector tiles at grid edges. Empty ocean outside the extracted geographic coverage is not advertised as downloadable coverage. `plan.json` lists every expected grid; publication fails if a batch is missing. The app downloads full files and does not use remote Range requests.

Only small matrix JSON files use Actions artifacts (one-day retention); map binaries and reproducibility inputs use Releases. Failed jobs can be rerun. Equal existing assets are retained; different bytes fail without clobbering them. A country/source release and all grid releases must be complete before the manifest is activated.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python pipeline.py plan --country TW --scope poc --version 2026-09-poc1
python pipeline.py build --batch TW-X-Y --source-tag SOURCE-TW-2026-09-poc1
python pipeline.py assemble --scope poc --version 2026-09-poc1 --output manifest.json
```

Dependencies for generation: Java 21, osmium-tool CLI, GitHub CLI authenticated for this repository, and the pinned Python packages. Keep `GH_TOKEN` in the workflow environment. Never include tokens or private route coordinates in files, logs, releases, or manifests.

## Data and attribution

© OpenStreetMap contributors. Map data is made available under the Open Database License (ODbL) 1.0: https://www.openstreetmap.org/copyright and https://opendatacommons.org/licenses/odbl/1-0/.

The extracted OSM PBF inputs and generated PMTiles are OSM-derived databases distributed under ODbL 1.0. Keep attribution and the license notice with redistribution. The custom schema is `kaeroute-v1`; this is not an OpenMapTiles profile. Planetiler is an Apache-2.0 tool; the pipeline does not redistribute its executable. The app and PDF must visibly display OSM attribution (including the copyright URL on printed maps).

Maps describe ordinary geographic data. They do not guarantee safety, passability, facility opening, or that travel should begin after a disaster.

## Validation status

`generation-report.json` records aggregate file counts and capacities after a successful build. ORS validation requires a separately configured API key, and real-device / print validation is recorded in the app repository. A successful map build alone does not mean these validations passed or that the app is released.
