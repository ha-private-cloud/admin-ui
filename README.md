# admin-ui

The admin console for ClusterKeep, behind Cloudflare Access. Admins use it to see every account and decide what each person can do.

## What it does

- Lists every account with its email and role.
- Makes someone an admin or a normal user.
- Sets each person's cluster access to none, view, edit or admin. Only members of `cluster-admins` see this, and nobody can change their own access.
- Anyone who isn't signed in as an admin gets the 404 page, so the console doesn't give away that it exists.

admin-ui doesn't store anything itself. For each action it mints a short-lived service token from auth-api using the admin's session, then calls auth-api's admin API with it. auth-api checks the token's groups again, so cluster access can only be changed by a cluster admin even if someone calls the API directly.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/` | List of users |
| POST | `/users/<user_id>/role` | Make someone an admin or a normal user |
| POST | `/users/<user_id>/cluster-role` | Set someone's cluster access |
| GET | `/healthz` | Kubernetes health check |

## Technologies

- Python 3.13 and Flask, served by gunicorn
- Jinja2 templates styled with Tailwind CSS, built ahead of time
- requests for the calls to auth-api
- pytest with the responses library for the tests
- uv for dependencies, Docker for the image, Helm and OpenTofu for deployment

## Configuration

| Variable | What it's for |
|---|---|
| `AUTH_API_INTERNAL_URL` | auth-api's address inside the cluster |
| `CLUSTERKEEP_UI_URL` | clusterkeep-ui's address, for log out and the 404 page |
| `STORAGE_UI_URL` | storage-ui's address, for the link back to storage |
| `HEADLAMP_URL` | Headlamp's address, for the link to the cluster dashboard |
| `APP_TITLE` | Site title, defaults to ClusterKeep |

## Running locally

The easiest way is [common-dev-env](https://github.com/LandRegistry/common-dev-env) with the [dev-env-config](https://github.com/ha-private-cloud/dev-env-config) repository, set up as the [organisation README](https://github.com/ha-private-cloud/.github/blob/main/profile/README.md#running-it-locally) describes. That runs it on http://localhost:9083. You can also run it on its own.

```sh
uv sync
npm install
npm run build:css
uv run flask --app app run --debug
```

Run the tests with `uv run pytest`.

## Deployment

The GitHub workflows come from the shared `pipelines` repo.

- Pushes to `development` and `preview` deploy to dev and preview.
- Merges to `main` build the image without deploying it.
- Pushing a `vX.Y.Z` tag builds the release image, and running the release workflow with that tag deploys it to production.

`preview` and `main` only take changes through pull requests. The Helm chart is in `charts/admin-ui` and the OpenTofu config is in `tofu/`. Production is public through the Cloudflare Tunnel, with Cloudflare Access in front of it.
