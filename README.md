# supe-ask

Python Ask control plane for Supe Market.

This service is intended to live as its own standalone repository alongside the
other split Supe services.

## Responsibilities

- authenticate Ask requests through `auth-service`
- persist Ask threads, runs, events, and artifacts in PostgreSQL
- run retrieval and code generation
- execute generated Python through the isolated ECS runner backend

## Local Development

Create a virtual environment, install dependencies, and start the API:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 3020 --app-dir src
```

Required runtime dependencies:

- PostgreSQL reachable through `ASK_DATABASE_URL` or `DATABASE_URL`
- a reachable `auth-service`
- `UMS_AUTH_PARAM` from the auth bootstrap flow
- Vertex AI credentials if you want the Ask service to pass startup provider validation

## Build Images

- control plane image: `Dockerfile`
- isolated runner image: `runner.Dockerfile`

If the ECS runner is launched on `linux/amd64`, build and push the runner image
with an amd64 or multi-arch manifest. Building and pushing from Apple Silicon
without `buildx --platform ...` can produce an arm64-only image that Fargate
cannot pull.

Example:

```bash
docker buildx build \
  --platform linux/amd64 \
  -f supe-ask/runner.Dockerfile \
  -t <your-ecr-runner-image>:<tag> \
  --push \
  .
```

## Execution Model

Ask execution is ECS-only. There is no local fallback for generated analysis.

The default production shape is `codebox`: the control plane enqueues work into
SQS and a long-lived ECS worker service keeps a warmed Python process ready.

The control plane still owns retrieval, code generation, persistence, and SSE.
Only the generated Python execution moves into the isolated runner image.

## Codebox Execution Model

`supe-ask` now supports two ECS dispatch modes:

- `codebox` mode when `ASK_CODEBOX_QUEUE_URL` is set
  - the control plane uploads the input manifest to S3, then sends a queue
    message
  - a long-lived ECS service runs `python -m supe_ask.codebox_worker`
  - each worker pre-warms a Python process so the next run starts without
    cold-import cost
- one-off task mode when `ASK_CODEBOX_QUEUE_URL` is not set
  - the control plane launches a fresh Fargate task per run
  - keep this for smoke tests or if you explicitly want per-run tasks

The warmed `codebox` path is the closest equivalent to ScalarField's always-on
runner model.

## ECS Runner Requirements

Use ECS mode only when the runner tasks can reach the same PostgreSQL/RDS
instance directly.

- treat the EC2-hosted control plane and the ECS runner task as separate network identities
- run the ECS tasks in subnets that can reach RDS and the internal Ask callback URL
- add an RDS inbound rule from the runner task security group on `tcp/5432`
- inject a read-only `ASK_DATABASE_URL` into the runner task definition or secret store
- set `DB_SSL=true` when the RDS instance requires TLS
- point `ASK_CONTROL_PLANE_INTERNAL_URL` at a VPC-reachable Ask endpoint such as an internal ALB or private DNS name
- do not use `localhost`, Docker Compose hostnames, or public-only hostnames for `ASK_CONTROL_PLANE_INTERNAL_URL`

For the warmed `codebox` path, the control plane requires:

- `ASK_CODEBOX_QUEUE_URL`
- `ASK_CONTROL_PLANE_INTERNAL_URL`
- `ASK_RUNNER_INPUT_BUCKET`
- `ASK_RUNNER_ARTIFACT_BUCKET`

The codebox worker task definition requires:

- `ASK_CODEBOX_QUEUE_URL`
- `ASK_DATABASE_URL`
- `DB_SSL`
- `ASK_CONTROL_PLANE_INTERNAL_URL`
- `ASK_RUNNER_INPUT_BUCKET`
- `ASK_RUNNER_ARTIFACT_BUCKET`
- `AWS_REGION`

Optional worker tuning:

- `ASK_CODEBOX_POLL_WAIT_SECONDS`
- `ASK_CODEBOX_VISIBILITY_TIMEOUT_SECONDS`
- `ASK_CODEBOX_QUEUE_STALE_SECONDS`
- `ASK_CODEBOX_WARM_POOL_SIZE`
- `ASK_CODEBOX_WARM_POOL_MAX_USES`
- `ASK_CODEBOX_WARM_READY_TIMEOUT_SECONDS`

The control plane still requires these settings for one-off ECS smoke tasks:

- `ASK_ECS_CLUSTER`
- `ASK_ECS_TASK_DEFINITION`
- `ASK_ECS_SUBNETS`
- `ASK_ECS_SECURITY_GROUPS`
- `ASK_CONTROL_PLANE_INTERNAL_URL`
- `ASK_RUNNER_INPUT_BUCKET`
- `ASK_RUNNER_ARTIFACT_BUCKET`

## ECS Runner Smoke Checks

Two repo-native smoke helpers are available before switching staging traffic:

- `python -m supe_ask.runner_preflight`
  - run this inside the runner image or as an ECS command override
  - validates RDS connectivity, internal Ask health reachability, and S3 round-trips
  - optionally validates Secrets Manager access when `ASK_RUNNER_SECRET_PROBE_ID` is set
- `python -m supe_ask.runner_smoke_task`
  - launch a one-off Fargate task using the configured `ASK_ECS_*` settings
  - overrides the runner command to execute `supe_ask.runner_preflight`
  - waits for task completion and prints task status JSON to stdout

Typical staging sequence:

1. Build and push the runner image for `linux/amd64` or a multi-arch manifest.
2. Wire the task definition with the read-only `ASK_DATABASE_URL`, `DB_SSL`, and runner IAM/network settings.
3. Run `python -m supe_ask.runner_smoke_task` from an environment with the staging `ASK_ECS_*` configuration.
4. Inspect CloudWatch logs for the preflight JSON results.
5. Start the codebox ECS service only after the smoke task proves RDS and control-plane reachability.
6. Flip the control plane to `ASK_CODEBOX_QUEUE_URL=<queue-url>` and restart it.

## AWS Setup Steps

Use this order for staging:

1. Create an SQS queue for codebox jobs.
   Use a visibility timeout comfortably above `ASK_RUN_TIMEOUT_SECONDS`.
2. Build and push the runner image for `linux/amd64` or a multi-arch manifest.
3. Create a dedicated ECS task security group for the codebox service.
   Allow egress to RDS, the internal Ask endpoint, S3, ECR, CloudWatch Logs,
   and Secrets Manager through your NAT or VPC endpoints.
4. Update the RDS security group.
   Add inbound `tcp/5432` from the codebox worker security group. Keep the EC2
   rule in place.
5. Create or reuse a read-only database secret for the worker.
   Expose it to the ECS task definition as `ASK_DATABASE_URL`. Set `DB_SSL=true`
   if the RDS instance requires TLS.
6. Create the ECS task definition for the codebox service.
   Use the runner image and override the command to:

```bash
python -m supe_ask.codebox_worker
```

7. Inject these worker environment variables into the task definition:
   `ASK_CODEBOX_QUEUE_URL`, `ASK_CONTROL_PLANE_INTERNAL_URL`,
   `ASK_RUNNER_INPUT_BUCKET`, `ASK_RUNNER_ARTIFACT_BUCKET`, `AWS_REGION`,
   plus any S3 endpoint overrides you intentionally use.
8. Give the worker task role these permissions:
   `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`,
   `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, CloudWatch Logs write,
   ECR pull, and Secrets Manager read if the DB URL comes from Secrets Manager.
9. Give the control-plane instance or task role these permissions:
   `sqs:SendMessage` on the same queue and S3 write access for the input bucket.
10. Deploy an ECS service with desired count `>= 1`.
    Start with `1` for a single always-warm codebox and scale horizontally by
    increasing desired count.
11. Run `python -m supe_ask.runner_smoke_task` against the same VPC/subnets/SGs.
12. Once the smoke task passes, set `ASK_CODEBOX_QUEUE_URL` on the control plane and restart `supe-ask`.
13. Submit a real Ask run and verify:
    the run enters `queued`, the worker picks it up quickly, heartbeats arrive,
    artifacts persist, and the queue depth returns to zero.

## Runtime Notes

- SQL migrations run automatically on startup.
- `/health` reports liveness.
- `/health/ready` reports provider-readiness status.
- `/api/v1/ask/internal/health` now reports `executionMode=codebox` when the
  queue-backed warmed worker path is enabled.
- In both dev and staging, the Ask service can connect to PostgreSQL outside
  Docker as long as the configured database URL is reachable.
