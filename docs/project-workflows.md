# Project workflows

## Run a reviewed intake without a model

1. Open **Tables and schedules**. Create a table called **Requests** with the columns **Request, Status**. Add a row or paste CSV with the same header. Use **Add a typed column** for numbers, dates, booleans, URLs, email addresses or a fixed list of choices.
2. Choose **Create a reviewed intake workflow**. The studio fills in a Start, a human review, an insert into this exact table, and the approved and declined paths. Save the version. The run input contains sample JSON using your column names.
3. Select **Test version** and approve the row. Inspect the receipt and reopen the table. Testing uses a disposable copy of project storage. **Publish version**, then **Run published version** and approve a live row. Find it in the table and download the CSV.
4. To run on a clock, return to **Tables and schedules** and select that published version. Choose an interval or enter a cron expression and IANA time zone. Supply a JSON row with your columns, preview the dates, then enable the trigger. Each scheduled intake waits for its own approval. **Pause** stops future admissions. Open a delivery to see the exact run and version.

Use **Create project** to separate tables, documents, files, schedules and workflow libraries. The owner key can access every project in the installation. Projects provide organization and data scope for one trusted owner.

## Draft with a model

Select **New**, choose a registered model and describe the steps and review policy. To draft against existing resources, select **Include table names, columns, IDs and published workflow names in the drafting request**. Penstock sends that metadata and the brief to the selected model. An example brief is *“Review each incoming JSON row. When I approve, insert it in Requests. On decline, end the run. Return the saved row.”*

Open **Available block configurations** for the installed syntax. Simple fields can hold JSON literals. In blocks that accept bound inputs, `{"ref":{"node":"$input","path":["order","id"]}}` reads a nested input. Use a step ID for a completed step, `$previous` for the incoming branch value or `$variables` for named values. Array paths take numeric indexes. A missing value fails the step and appears in the receipt. Wrap a literal shaped like a reference with `{"literal":...}`.

## Blocks

| Blocks | Behavior |
| --- | --- |
| `start`, `finish`, `response` | Accept free-form input or a typed Start form. Resolve a response value for downstream steps or Finish. |
| `agent`, `local-model`, `chat`, `thinking` | Send instructions and the current input to the selected model profile. Agent also accepts a strict output schema. Model and skill selections are recorded. |
| `condition`, `variables`, `verify-value`, `guardrails` | Compare values, set named variables, compare an expected JSON value or validate a schema, and route JSON or RE2 regex checks through success or error. |
| `translate`, `evaluator`, `router`, `rank`, `decisions`, `vision` | Translate to a language, score against criteria, select a declared route, rank an array, route a probability against a threshold, or ask a vision-capable model about a project image. |
| `review`, `human-input`, `wait` | Wait for an approval, collect a JSON answer with an optional object schema, or resume after a specified number of seconds. Receipts survive a restart. |
| `table`, `pinned-table` | Query, get schema, insert, upsert, batch insert, update or delete rows. A pinned table records a small, exact revision when the version is saved. Use paged queries for larger tables. |
| `file`, `knowledge`, `memory`, `step-log`, `callback` | Read or write project text files, manage versioned local documents and chunks, maintain conversation messages, write or inspect the project log, and record local callback messages. |
| `function` | Run bounded JavaScript in QuickJS with JSON parameters and JSON output. A block has a one-second execution budget and a 16 MiB VM memory budget. |
| `service`, `http`, `rss`, `mcp`, `a2a` | Preview and approve an operation on a registered service. Use JSON GET/POST, RSS or Atom, MCP discovery and allowlisted tool calls, or A2A agent cards, messages and task operations. |
| `credential`, `deployments` | Inspect registered service handles, list workflow versions, or request approval to publish a tested version or withdraw it. Secret values stay in the server process. |
| `invoke-workflow`, `for-each`, `parallel-each` | Call a specific published child version in the same project. An array input can launch one child per item. Results follow input order. Open child receipts to resolve their reviews. |
| `loop-container`, `parallel-container` | Supply a nested workflow plan in `body` and an `items` collection or `count`. Each iteration receives `{"index":0,"item":...}` and has its own variables and receipt. Saving and publishing the parent governs the embedded body. |

Each collection accepts up to 100 items. A top-level parallel collection admits up to four children at once. Nested collections execute serially. A run family can contain up to 500 runs. Project tests share one disposable database across their children. Service blocks preview their inputs during tests. Set a service block’s optional `preview` to a representative JSON response when later steps need fields returned by that service. The receipt labels the simulated delivery. Model calls, including vision, reach the selected model during a test and may incur provider charges.

## Scheduling and events

The scheduler runs inside `penstock.py serve`. Keep the server running to dispatch work. A fixed interval measures elapsed seconds. A calendar schedule uses the selected time zone and Croner. The preview displays upcoming occurrences in the browser's local time. After downtime, a clock admits one overdue occurrence, then advances to the next future one. A deterministic delivery ID prevents the same occurrence from starting twice. Delivery creation and schedule advancement commit together. A committed delivery can complete after a trigger is paused.

Table triggers receive the operation, row ID, typed row, previous row and changed columns when available. Project change triggers observe resource edits through the project API. Webhook triggers show the endpoint and a bearer token once. Store the token privately and POST an application/json object with `Authorization: Bearer <webhook-token>` and a stable `Idempotency-Key`. Repeating an event with the same ID and payload returns the existing delivery. To replace a webhook token, pause its trigger and create another.

RSS and service polling establish a baseline from the first successful response. Later polls dispatch newly observed item IDs. For a JSON service, register a fixed GET endpoint and choose the keys leading to its list and the name of its stable ID field. The receipts and observed IDs persist across restarts. Failed polls retry with backoff. Changing the registered service configuration disables its polling trigger until you create a new one.

## Service setup

The configuration template contains optional profiles for each protocol. Start the endpoints you want to use, choose a model ID, and store credential values in the named environment variables. Choose the final URL for each service. Add a short `description` to tell the drafting model what a service does. A JSON HTTP profile can also declare a strict object `input_schema` in TOML. The studio validates the payload before requesting delivery approval. For example, register an HTTP adapter for an existing search, transcription or business system and describe the required inputs there. The MCP profile's `tools` array determines which discovered tools may be called. An A2A profile specifies the exact additional interface URLs the agent card may select. List only addresses authorized to receive that profile's credential. Model capabilities such as image support depend on the configured endpoint. [Connection examples](../examples/connections.toml)

For model-free operation, all project storage, imports, human reviews, child workflows and fixed schedules run locally. Calendar schedules require Node.js and time zone data. Function, Guardrails, RSS, MCP and A2A also require the pinned npm packages. The Compose image installs them. For a native installation, use Node.js 22 or newer and run `npm ci --ignore-scripts`. Croner is included under its own license. Installed npm dependencies retain their respective licenses. [Croner notice](../server/vendor/CRONER-LICENSE.txt)

## Project API

All studio APIs require the owner bearer token. The following JSON requests use POST. The workspace endpoint also accepts authenticated GET. Mutation keys are UUIDs chosen by the caller. Reuse a key only to retry the same change.

| Path | Request or result |
| --- | --- |
| `/api/project-create`, `/api/project` | Create with `{"name":"Research"}`. Open with `{"project":"<id>"}` to see versions, runs and available block fields. |
| `/api/resources` | Supply the project ID to list its tables, files, knowledge documents, logs and callbacks. |
| `/api/resource` | Send `{"project":"<id>","action":"table-read","data":{"id":"<table-id>","limit":25,"offset":0}}`. |
| `/api/resource` | Create a table with action `table` and data containing a mutation `key`, `name` and `columns`. Each column has a name and optional type, unique flag and select options. |
| `/api/resource` | Use `table-row`, `table-cell`, `table-column`, `table-view`, `table-row-delete`, `table-column-delete`, `table-rename`, `table-import` or `table-export` for subsequent changes. Read the current table and row revisions before editing. |
| `/api/triggers`, `/api/schedule-preview` | List a project's triggers and recent deliveries, or preview five occurrences for a timing configuration. |
| `/api/trigger-create`, `/api/trigger-enable` | Create with project, workflow, version, kind, config and optional input. Pause or resume using project, trigger ID and boolean enabled. |

The Community Edition is a work in progress. Workflows use the public plan format. Machine-specific desktop operators, vendor-native service actions and multi-user access controls still require separate integration work. The registered HTTP, MCP and A2A adapters let workflow authors call the services their installation already provides.
