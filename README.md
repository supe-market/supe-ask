# supe-ask

Python Ask control plane for Supe Market.

This service is intended to live as its own standalone repository alongside the
other split Supe services.

## Responsibilities

- authenticate Ask requests through `auth-service`
- persist Ask threads, runs, events, and artifacts in PostgreSQL
- run retrieval and code generation
- execute generated Python either locally or through the optional ECS runner backend

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

## Execution Modes

- `ASK_RUNNER_BACKEND=local`
  - default mode
  - generated Python runs inside a local subprocess started by the control plane
- `ASK_RUNNER_BACKEND=ecs`
  - optional isolated execution mode
  - requires ECS, S3, callback connectivity, and task-definition level database credentials

When ECS mode is enabled, the control plane still owns retrieval, code generation, persistence, and SSE. Only the generated Python execution moves into the ephemeral runner.

## Runtime Notes

- SQL migrations run automatically on startup.
- `/health` reports liveness.
- `/health/ready` reports provider-readiness status.
- In both dev and staging, the Ask service can connect to PostgreSQL outside Docker as long as the configured database URL is reachable.
