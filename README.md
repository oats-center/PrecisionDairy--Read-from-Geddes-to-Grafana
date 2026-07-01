#Precision Dairy: Read from Geddes to Grafana
Containerized data-processing pipeline for the Purdue Precision Dairy dashboard workflow. The container reads mounted data from the cluster, generates Chapter 3 herd-characteristics tables and Chapter 4 feed-traits tables, saves CSV backup outputs, and optionally loads the generated outputs into PostgreSQL for Grafana visualization.
##Repository contents
```text
.
├── Containerfile
├── requirements.txt
├── run_all.py
├── chapter3_herd_pipeline.py
├── chapter4_feed_pipeline.py
├── load_chapter_outputs_to_postgres.py
└── README.md
```
##What this container does
The container runs the following workflow:
Process Chapter 3 herd-characteristics data.
Process Chapter 4 feed-traits data.
Write CSV backup files to `/outputs/chapter3` and `/outputs/chapter4`.
Load generated CSV outputs into PostgreSQL, when PostgreSQL environment variables are provided.

##Expected container paths
The Python scripts use paths inside the container. The real cluster paths are mounted into these locations by IT when the container is run.
Container path	Purpose
`/data/afi`	Input folder for Afimilk/Chapter 3 files
`/data/allWeights.csv`	Input body-weight file for Chapter 3
`/data/feed-intake`	Input folder for TMR Tracker/feed-intake files
`/data/nutrient-table.csv`	Input nutrient-composition table for Chapter 4
`/outputs/chapter3`	CSV backup outputs for Chapter 3
`/outputs/chapter4`	CSV backup outputs for Chapter 4
Do not hard-code real cluster paths such as `/depot/...` inside the Python scripts. Those paths should be supplied only through Podman/Docker volume mounts.


##PostgreSQL configuration
The PostgreSQL database is external to the container, typically hosted on the cluster. The container connects to it using environment variables.
Required environment variables:
```bash
POSTGRES_HOST=
POSTGRES_PORT=5432
POSTGRES_DB=
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_SCHEMA=
```
Do not commit real passwords or `.env` files to GitHub.


##Build the container
From the repository root:
```bash
podman build -t precision-dairy-pipeline .
```
Docker can also be used if needed:
```bash
docker build -t precision-dairy-pipeline .
```


##Run the full workflow
In production, IT should run the container on the cluster and mount the real cluster data locations into the expected container paths.
Example Podman command:
```bash
podman run --rm \
  --env-file postgres.env \
  -v /real/cluster/afi/path:/data/afi:ro \
  -v /real/cluster/allWeights.csv:/data/allWeights.csv:ro \
  -v /real/cluster/feed/path:/data/feed-intake:ro \
  -v /real/cluster/nutrient-table.csv:/data/nutrient-table.csv:ro \
  -v /real/cluster/outputs:/outputs \
  precision-dairy-pipeline
```
The default command in the container runs:
```bash
python run_all.py
```


##Dry run
To check the workflow commands without processing data:
```bash
podman run --rm precision-dairy-pipeline python run_all.py --dry-run
```


##Run selected parts
Run Chapter 3 only:
```bash
python run_all.py --skip-chapter4 --skip-postgres
```
Run Chapter 4 only:
```bash
python run_all.py --skip-chapter3 --skip-postgres
```
Run both chapters but do not load PostgreSQL:
```bash
python run_all.py --skip-postgres
```
Load existing Chapter 3 and Chapter 4 CSV outputs into PostgreSQL:
```bash
python load_chapter_outputs_to_postgres.py \
  --chapter3-dir /outputs/chapter3 \
  --chapter4-dir /outputs/chapter4 \
  --if-exists replace
```

##Output tables
Chapter 3 expected PostgreSQL tables
```text
chapter3_yield_and_dim
chapter3_dim_weights_merged
chapter3_yield_group_day
chapter3_yield_group_month
chapter3_weight_group
chapter3_yield_per_cow
```
Chapter 4 expected PostgreSQL tables
```text
chapter4_filtered_feed_intake
chapter4_dry_weight_participation
chapter4_weight_difference_ing
chapter4_error_by_weight
chapter4_nutrient_delivery_by_ingredient
chapter4_nutrient_error_by_formula
```


##Development notes
This repository should contain only code and container configuration. Do not commit raw farm data, generated CSV outputs, PostgreSQL credentials, or cluster-specific secrets.
Recommended `.gitignore` entries:
```text
__pycache__/
.ipynb_checkpoints/
outputs/
*.env
postgres.env
*.csv
```
If a small reference CSV is intentionally needed for testing, place it in a dedicated test-data folder and document it clearly.


##Troubleshooting
`localhost` does not connect to PostgreSQL
Inside a container, `localhost` means the container itself. If PostgreSQL is running on the cluster host, IT should either run the container with host networking or provide the correct PostgreSQL hostname/service address.
No files found
Check that the real cluster folders were mounted into the correct container paths. For example, the script expects feed-intake data at `/data/feed-intake`, not at the original `/depot/...` path.
CSV outputs are created but PostgreSQL tables are missing
Confirm that all `POSTGRES_*` environment variables are available inside the container and that the database user has permission to create or replace tables in the target schema.
