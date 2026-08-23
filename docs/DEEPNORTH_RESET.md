# DeepNorth reset

DeepNorth currently contains disposable PostgreSQL test data. After the SQLite-default image is published, the existing Northbound stack and test data can be removed and redeployed using the normal `compose.yaml` workflow.

The reset is intentionally separate from application development because it deletes the current PostgreSQL database and uploaded test media. Resolve the exact deployed Compose project and persistent paths immediately before performing the reset.

After redeployment, verify:

- first-run setup;
- SQLite at `/data/northbound.sqlite3`;
- profile-picture upload and retrieval;
- persistence across container recreation;
- LAN access at `192.168.0.11:8060`;
- public access through `northbound.deepnorth.app`.
