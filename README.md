# agentic-hpc-dashboard

The aims of this repository is to collect agent behaviors on HPC login nodes (the collector), and make them visible to human (the dashboard).

## Quick start -- Collector (`/collector`)
A single file (`ebpfm.sh`) acts as a single entry point for the collectors.
Its commands are:
```bash
sudo ./collector/ebpfm.sh bootstrap    # dependencies + the two sysctls, once per node
sudo ./collector/ebpfm.sh check        # expect 0 FAIL
sudo ./collector/ebpfm.sh start        # start the collector
sudo ./collector/ebpfm.sh status       # check the collector runtime
sudo ./collector/ebpfm.sh stop         # stop the collector
```

## Quick start -- Dashboard (`/dashboard`)
Step-by-step
```bash
cd dashboard && uv sync
uv run python -m rc_dashboard --check-feeds       # what resolved, what didn't, and why
cd web && npm ci && npm run build && cd ..        # builds web/dist/, which the service serves
uv run python -m rc_dashboard serve --port 8080
```

Bash script bundling
```bash
bash run.sh
```

## Configuration -- Dashboard
```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm        # Where the ebpf collected data reside
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login  # Where the other data reside (node-level)
```
