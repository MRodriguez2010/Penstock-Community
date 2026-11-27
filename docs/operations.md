# Connections, notifications and recovery

From the studio, open **Connections**. A workspace owner can add model profiles and instruction skills. Supply the complete endpoint URL and the model ID served by that endpoint, then choose the profile in a workflow. Use HTTPS for a remote endpoint. HTTP on a numeric loopback address supports a local model. The optional credential field names a server environment variable starting with `PENSTOCK_PROFILE_`. Existing workflows keep their configured profile. Give a changed model or skill a new name and test a new workflow version.

## Build a brief from a project table

Open **Weekly executive brief**, choose **Your data**, then **Use a project table**. Select a table with Date, Title (or Update), and Status columns. Owner, Due, Decision, and Source are optional. Penstock loads up to 40 updates, shows the period and source, and lets you edit the local copy before you preview it. The review receipt retains the inputs used for that run.

## Provision a project connection

IT creates `operations.toml` next to the workspace's `owner.key`, with one table per authorized project and capability. Supply the actual project ID and supply secrets through the service environment or a secret manager. The studio only shows connections available to that project. The following example uses placeholder names.

```toml
[projects."YOUR-PROJECT-ID".microsoft-mail]
token_env = "OUTLOOK_DELEGATED_ACCESS_TOKEN"

[projects."YOUR-PROJECT-ID".google-drive]
token_env = "GOOGLE_DRIVE_ACCESS_TOKEN"

[projects."YOUR-PROJECT-ID".aws-s3]
region = "us-east-1"
bucket = "your-bucket"
prefix = "approved-input/"

[projects."YOUR-PROJECT-ID".aws-bedrock]
region = "us-east-1"

[projects."YOUR-PROJECT-ID".slack]
webhook_env = "PROJECT_SLACK_WEBHOOK"
studio_url = "https://penstock.example.org"

[projects."YOUR-PROJECT-ID".teams]
webhook_env = "PROJECT_TEAMS_WORKFLOW_WEBHOOK"
studio_url = "https://penstock.example.org"

[projects."YOUR-PROJECT-ID".email]
host = "smtp.example.org"
port = 465
sender = "penstock@example.org"
recipient = "reviews@example.org"
username = "penstock"
password_env = "PROJECT_SMTP_PASSWORD"
studio_url = "https://penstock.example.org"
```

Use `google-mail` for Gmail and `microsoft-files` for OneDrive. Grant the narrow delegated permission for the requested read. Use a project IAM role or named AWS profile for S3 listing and Bedrock model discovery. The AWS adapters use the optional packages in `requirements-aws.txt`. The workflow’s **Use in a workflow** action binds the chosen connection settings to a draft. A sample preview uses example items. A live read identifies the source and pauses for approval. A changed or revoked binding needs a new draft.

## Install a typed lookup adapter

IT can register typed lookup adapters in the workspace's `connections.toml`. Each adapter is a service operated or approved by IT that implements the request and response contract below. Use the project ID from the owner-authenticated `/api/workspace` catalog. Restrict each adapter to the projects authorized to send it queries. Restart the service after changing the configuration.

```toml
[lookups.team-search]
kind = "web-search"
url = "https://lookup.example.org/search"
projects = ["YOUR-PROJECT-ID"]
key_env = "PENSTOCK_SEARCH_ADAPTER_TOKEN"

[lookups.team-repositories]
kind = "github"
url = "https://lookup.example.org/repositories"
projects = ["YOUR-PROJECT-ID"]

[lookups.team-directory]
kind = "enrichment"
url = "https://lookup.example.org/directory"
projects = ["YOUR-PROJECT-ID"]

[lookups.team-health]
kind = "service-health"
url = "https://lookup.example.org/health"
projects = ["YOUR-PROJECT-ID"]
```

Penstock sends one HTTPS JSON POST to the registered endpoint with `operation`, `query` and `limit`. Loopback HTTP is available for local development. Set any optional bearer credential through the server environment. There are no redirects, pagination or automatic retries. Penstock accepts `{"items": [...]}` with up to the requested number of records, each with a `sourceUrl` containing an HTTPS evidence address. Adapter credentials and raw responses stay out of the run receipt. The adapter selects and normalizes records from its upstream service and must limit data to the fields listed below.

| Operation | Query | Required item fields besides `sourceUrl` | Optional fields |
| --- | --- | --- | --- |
| `search` | Search text | `title` | `snippet` |
| `repository` | `owner/name`, limit one | `name` | `description`, `defaultBranch` |
| `issues` | `owner/name` | `title`, integer `number` | `state` (`open`, `closed`) |
| `pull-requests` | `owner/name` | `title`, integer `number` | `state` (`open`, `closed`, `merged`) |
| `person` | Professional work email | `name` | `jobTitle`, `company` |
| `company` | Domain | `name` | `domain`, `industry`, `description` |
| `service-health` | Service ID | `service`, `status` (`healthy`, `degraded`, `unavailable`, `unknown`), `checkedAt` | Numeric `latencyMs` (0–300,000) |
| `worker-status` | Worker ID | `worker`, `status` (`ready`, `busy`, `offline`, `unknown`), `observedAt` | |
| `published-workflow` | Workflow ID | `workflow`, `version` as text, `publishedAt` | `name` |
| `weather-observation` | Station ID | `station`, `observedAt`, numeric `temperatureC` (−100 to 70) | Numeric `humidityPercent` (0–100), `windSpeedMps` (0–150), `pressureHpa` (300–1,150) |

Repository details and each of the four infrastructure and weather operations take a limit of one. For `service-health`, `worker-status`, `published-workflow` and `weather-observation`, use the operation name as the adapter kind and have the adapter resolve an approved resource ID. An ID starts with a letter or digit. After that it can contain letters, digits, underscores, hyphens and periods, with a maximum of 80 ASCII characters. The configured adapter provides read-only status, publication metadata or a measured station observation. Each timestamp requires a timezone, such as `2026-11-27T14:00:00Z`, and may be up to five minutes ahead of the Penstock clock for clock skew. Older observations keep their original measurement time.

Other lookups accept one to ten results. Queries are single lines of up to 300 UTF-8 bytes. Result text fields have per-field bounds. For example, titles take up to 160 bytes and snippets and descriptions up to 500. Receipts include the selected adapter, operation and result source URLs, plus a retrieval timestamp for a live call.

Open the workflow editor and choose a lookup under **Insert step**. The available lookup blocks depend on the selected project's adapters. Enter a query or bind the workflow input or preceding step result. Offline and model tests return labeled sample records without contacting the adapter. To use the live adapter, publish the tested version, select **Allow this live run to read from configured external services**, and have a reviewer approve the displayed input and destination. A changed adapter registration requires saving and testing a new version. Live queries may contain work email addresses, so use an adapter approved to receive that data.

## Project notifications

Notification channels require a workspace owner to enable delivery. Project operators can open the inbox and inspect delivery status. The email, Teams, and Slack messages carry the event type and a sign-in link. Open the result to see its current state and decide. A delivery whose remote outcome is uncertain stays in the inbox for investigation. Samples and local starter approvals stay in the project. Changing a recipient or webhook requires fresh owner confirmation. Older events without a confirmed destination remain in the inbox.

## Back up and rehearse a restore

Stop the server or desktop app before taking a maintenance backup. Run `python admin/backup.py backup PATH_TO_WORKSPACE PRIVATE_BACKUP.zip`. The command obtains the workspace writer lock and refuses to overwrite a previous backup. Store the archive in encrypted storage with the same access restrictions as the workspace. It contains local credentials, user data and pending decisions. Back up your deployment secrets separately in your secret manager.

Run `python admin/backup.py restore PRIVATE_BACKUP.zip NEW_WORKSPACE_DIRECTORY` on an isolated machine. Start the restored version without live service credentials or outbound network access. Verify a saved result, a pending decision, project tables, local skills, and the next occurrence of an existing schedule. Stop the restored service before reconnecting production triggers. Keep one live scheduler for each workspace. For rollback, stop the new version and start the previous application with a copy restored to its matching schema.
