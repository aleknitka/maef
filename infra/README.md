# Infrastructure

No deployment manifests are shipped yet. MAEF runs as two processes on a machine you control: `maef api` and
`maef worker` (see [docs/RUNNING.md](../docs/RUNNING.md)); throwaway benchmark machines on AWS are created and
deleted by [`scripts/aws-bench/run.sh`](../scripts/aws-bench/run.sh).

Every API route requires a token, but the API is not yet hardened for exposure beyond `127.0.0.1` (no token expiry,
no rate limits, plain http accepted): see the known gaps in [docs/SECURITY.md](../docs/SECURITY.md).
