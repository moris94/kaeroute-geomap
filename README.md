# Kaeroute offline maps

Public geographic build pipeline for the 帰宅ルート iOS app. No application source or user data is published here.

Geofabrik dated OSM PBF → bounded osmium extracts → Planetiler 0.10.2 → grid PMTiles → GitHub Releases → manifest.json.

Run **Build offline maps** in Actions with scope `poc` first, then `full` with a new immutable version (for example `2026-09-v1`). Alternatively, commit an explicit `build-request.json` to request a run. Ordinary code changes do not trigger large map builds. The selected first full build uses approximately 10 km geographic grids, z8–16 and a 1 km route buffer; see `benchmark-results.json`. The 40 MB package goal is approximate: the measured urban maximum is 41.4 MB. Actual ORS route totals remain to be measured. All grid geometry is published in the manifest. Operations use geographic batch IDs, not prefectural boundaries. Each batch has at most 36 PMTiles assets, below the 900-file release target. Inputs use the dated 2026-09-01 Geofabrik extracts; checksums are recorded and verified. Never replace published assets: choose a new version when data, tools, grid or style changes.

The planning job derives occupied cells from OSM nodes within the pinned Geofabrik extraction extent, excluding distant complete-relation members, adds a full neighboring cell as margin, then performs complete-way/multipolygon extracts in bounded batches. Coast and border geometry comes from the same country input. PMTiles retain full intersecting vector tiles at grid edges. Empty ocean outside the extracted geographic coverage is not advertised as downloadable coverage. `plan.json` lists every expected grid; publication fails if a batch is missing. The app downloads full files and does not use remote Range requests.

Matrix batches are grouped into at most 128 jobs, four at a time, with work directories cleaned between batches. Only small matrix JSON files use Actions artifacts (one-day retention); map binaries and reproducibility inputs use Releases. Failed jobs can be rerun. Equal existing assets are retained; different bytes fail without clobbering them. A country/source release and all grid releases must be complete before the manifest is activated.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python pipeline.py plan --country TW --scope poc --version 2026-09-poc1
python pipeline.py build --batch TW-X-Y --source-tag SOURCE-TW-2026-09-poc1
python pipeline.py assemble --scope poc --version 2026-09-poc1 --output manifest.json
```

Dependencies for generation: Java 21, osmium-tool CLI, a repository-scoped `GH_TOKEN`, and the pinned Python packages. Keep `GH_TOKEN` in the workflow environment. Never include tokens or private route coordinates in files, logs, releases, or manifests.

## Data and attribution

© OpenStreetMap contributors. Map data is made available under the Open Database License (ODbL) 1.0: https://www.openstreetmap.org/copyright and https://opendatacommons.org/licenses/odbl/1-0/.

The extracted OSM PBF inputs and generated PMTiles are OSM-derived databases distributed under ODbL 1.0. Keep attribution and the license notice with redistribution. The custom schema is `kaeroute-v1`; this is not an OpenMapTiles profile. Planetiler is an Apache-2.0 tool; the pipeline does not redistribute its executable. The app and PDF must visibly display OSM attribution (including the copyright URL on printed maps).

Maps describe ordinary geographic data. They do not guarantee safety, passability, facility opening, or that travel should begin after a disaster.

## Validation status

`generation-report.json` records aggregate file counts and capacities after a successful build. ORS validation requires a separately configured API key, and real-device / print validation is recorded in the app repository. A successful map build alone does not mean these validations passed or that the app is released.

The **Compare grid and zoom sizes** workflow reuses published PoC source extracts to compare 10/15/20 km at z14/15/16 on fixed geographic areas. Reports are Actions artifacts and the selected measurements are recorded in `benchmark-results.json`. These are not fabricated walking routes and do not replace ORS acceptance tests.

`bounds/JP.poly` and `bounds/TW.poly` are Geofabrik extraction extents downloaded on 2026-09-09 from https://download.geofabrik.de/asia/japan.poly and https://download.geofabrik.de/asia/taiwan.poly. They define data coverage, not political boundary claims. Their checksums are recorded in source batch specifications. A newer explicit build request supersedes and cancels an unfinished run; partial assets never activate a manifest.

Generation renumbers OSM objects to dense derived IDs before multi-extraction. This preserves references while avoiding bitsets proportional to worldwide OSM IDs. These derived PBF files must never be uploaded back to OSM. The original source SHA-256 and dense source SHA-256 are both retained in batch specifications. Source assets are partitioned into releases with at most 400 PBF/spec pairs; the country source release holds the coverage plan and final plan.

A completed immutable `plan.json` allows later attempts to resume directly at tile generation. Change an `attempt` field in `build-request.json` to request another run without changing the data version; grid, scope, zoom and extraction boundary must still match the published plan. Individual builds always recheck the extracted PBF SHA-256. Changes to geographic inputs or map profile require a new version.

Publication streams each asset directly to the GitHub upload API, retaining the same repository token and respecting rate-limit reset/Retry-After headers. Permission errors fail immediately. Existing complete batches are reused only when every published asset size and server SHA-256 match its manifest. Release metadata and the pinned Planetiler JAR are reused across batches within a job. Bulk publication can take many hours because GitHub API quotas apply to the entire repository; retries wait instead of rotating credentials or clobbering assets.
