# Barbara GitHub Actions Sample

A minimal Python application that serves as a **template and demonstrator** for automating the full Barbara application lifecycle using GitHub Actions.

The goal is not a feature-rich app. It is a small, predictable artifact that shows how to wire GitHub Actions to the Barbara API for versioning, deployment, verification, and rollback.

## What This Repository Does

When a `v*` tag is pushed, the CI/CD pipeline runs end-to-end:

1. **Test** — runs the app's unit tests inside a Python container
2. **Validate deployment targets** — resolves `deployment-targets.json` against Barbara before building anything; fails fast if a declared device or group does not exist
3. **Build image** — builds and pushes a multi-platform Docker image tagged with the short commit SHA, the semver version, and `latest`
4. **Package artifact** — generates a release `.zip` containing a single `docker-compose.yaml` that references the immutable `<sha>`-tagged image
5. **Register Barbara application** — creates the application in Barbara if it does not already exist
6. **Upload Barbara version** — uploads the `.zip` as a new app version in Barbara
7. **Deploy to devices** — triggers deployment of that version to the targets declared in `deployment-targets.json`
8. **Verify deployment** — polls device health and rolls back if verification fails within the configured timeout
9. **Create GitHub release** — publishes a GitHub release with the `.zip` as the attached artifact, only after successful deployment verification

## Repository Structure

```
.
├── app/
│   └── main.py                          # HTTP app (port 8080)
├── scripts/
│   ├── barbara_api.py                   # Barbara and Keycloak request helpers
│   ├── resolve_deployment_targets.py    # Validates deployment-targets.json against Barbara
│   ├── package_docker_compose.py        # Generates the deployable docker-compose.yaml
│   ├── ensure_barbara_application.py    # Creates the Barbara app if missing
│   ├── upload_barbara_app_version.py    # Uploads the .zip as a Barbara app version
│   ├── deploy_barbara_app_version.py    # Triggers deployment on target devices
│   ├── verify_deployment.py            # Polls health and rolls back if needed
│   └── get_container_info.py           # Retrieves container runtime info from Barbara
├── tests/
│   └── test_app.py                      # Functional contract tests
├── .github/
│   └── workflows/
│       └── cicd.yml                     # Single CI/CD workflow
├── deployment-targets.json              # Declares target devices and groups
├── Dockerfile                           # Container definition
└── docker-compose.yaml                  # Local development / base for release artifact
```

## The Application

A minimal Python HTTP server with no framework dependencies:

| Route | Response |
|---|---|
| `/` | `Everything works fine, version <version>` |
| `/health` | `OK` |
| `/favicon.ico` | `204 No Content` |
| anything else | `404` |

The version exposed at runtime is assembled entirely from CI build arguments:

- `BARBARA_APP_VERSION` — derived from the git tag (e.g. `v0.1.0` → `0.1.0`)
- `BARBARA_GIT_SHA_SHORT` — the short commit SHA

Combined, the app exposes something like:

```
0.1.0+17a0484
```

This makes every running instance traceable back to an exact build and source commit. As a local development fallback, the app will attempt to resolve the SHA via `git rev-parse`; if git is unavailable, only the version is shown.

## The Release Artifact

The distributable artifact is a `.zip` containing a single `docker-compose.yaml`:

```yaml
services:
  sample-barbara-app:
    container_name: sample-barbara-app
    image: bdr.barbara.tech/public/github-actions-sample:17a0484
```

It references the immutable SHA-tagged image built by CI — not `latest`, not a locally rebuilt image. This artifact is what gets uploaded to Barbara as an app version and attached to the GitHub release.

## Deployment Targets

`deployment-targets.json` declares where each new version should be deployed:

```json
{
  "devices": ["device-a", "device-b"],
  "groups": ["group-a"]
}
```

Rules:

- `devices` entries are matched against `deviceName` in Barbara
- `groups` entries are matched against `name` in Barbara; all devices in a matched group are resolved
- devices from both lists are deduplicated
- if any declared device or group does not exist in Barbara, the pipeline fails **before** building the Docker image
- after successful validation, the job uploads `dist/resolved-deployment-targets.json` as a workflow artifact for diagnostics

## Local Development

Run the app directly:

```bash
python app/main.py
```

Run the tests:

```bash
python -m unittest discover -s tests -v
```

Run with Docker:

```bash
docker compose up --build
```

## GitHub Actions Configuration

The workflow is triggered by pushing a `v*` tag (e.g. `git tag v0.2.0 && git push --tags`).

### Repository variables

| Variable | Description | Example |
|---|---|---|
| `APP_NAME` | Logical application name in Barbara | `Github_Actions_Sample` |
| `IMAGE_NAME` | Image name in the registry | `github-actions-sample` |
| `REGISTRY_HOST` | Container registry host | `bdr.barbara.tech` |
| `REGISTRY_NAMESPACE` | Namespace within the registry | `public` |
| `APP_DEVELOPER` | Developer name in Barbara (defaults to `Barbara`) | `My Team` |
| `APP_SHORT_DESCRIPTION` | Short description for Barbara app registration | |
| `APP_LONG_DESCRIPTION` | Long description for Barbara app registration | |
| `APP_VERSION_ARCHITECTURES` | Comma-separated target architectures | `x86_64,arm64,armv7` |

Supported architecture values: `x86_64` → `linux/amd64`, `arm64` → `linux/arm64`, `armv7` → `linux/arm/v7`. Unrecognized values are ignored; if none remain, `x86_64` is used.

### Repository secrets

| Secret | Description |
|---|---|
| `REGISTRY_USERNAME` | Registry login username |
| `REGISTRY_PASSWORD` | Registry login password or token |

### Environment variables (per environment)

The workflow uses the `production` environment. Two environments are typically configured: `development` and `production`.

| Variable | Description | Example (production) |
|---|---|---|
| `BARBARA_API_URL` | Barbara API base URL | `https://prod.bap.barbara.tech` |
| `BARBARA_AUTH_URL` | Keycloak base URL | `https://prod.auth.barbara.tech` |
| `BARBARA_KEYCLOAK_REALM` | Keycloak realm | `bbr_prod` |
| `BARBARA_KEYCLOAK_CLIENT_ID` | Keycloak client ID | |
| `BARBARA_KEYCLOAK_USER_EMAIL` | User email for token requests | |
| `VERIFY_TIMEOUT_SECONDS` | Max seconds to wait for deployment health | `300` |
| `VERIFY_INTERVAL_SECONDS` | Poll interval during verification | `15` |

### Environment secrets (per environment)

| Secret | Description |
|---|---|
| `BARBARA_KEYCLOAK_CLIENT_SECRET` | Keycloak client secret |
| `BARBARA_KEYCLOAK_USER_PASSWORD` | Keycloak user password |

## Operational Notes

- The app version is driven entirely by the git tag. Pushing `v0.2.0` produces version `0.2.0+<sha>` at runtime.
- The only artifact suitable for deployment or rollback is the CI-built image. Do not substitute a locally rebuilt image.
- The `production` GitHub environment must have all Barbara and Keycloak variables and secrets defined for the pipeline to function.
- The `verify-deployment` job polls device status after deployment and automatically triggers a rollback if health is not confirmed within the timeout.
