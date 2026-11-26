# Workflow and operations guide

## Plans

A plan contains a name and up to 40 steps. IDs are unique. Each step has a type, optional display label, configuration, and outgoing connections. The compiler checks that the graph is connected and acyclic. You can inspect and edit the complete proposal under **Edit proposal**. [Working example](examples/review.json)

| Type | Configuration | Outputs |
| --- | --- | --- |
| start | Empty | success |
| agent | Registered provider, instructions, optional skill and response schema | success |
| condition | Text to match in `contains`, ignoring case | true, false |
| review | Question shown with the input | approved, declined |
| service | Registered service name | success after approval and a successful response |
| finish | Empty | Ends the current branch |

For a single success destination, use `"next":"step-id"`. To route an outcome, use `"next":{"approved":"send","declined":"hold"}`. To fan out, use `"next":{"success":["archive","summarize"]}`. Finish steps omit next. The value from the first selected incoming path becomes the input at a join. All executed steps keep their results. A failed model or service call stops execution. Test service delivery passes the reviewed input forward as its preview value. A live delivery passes the service response forward.

An Agent accepts an optional `schema` object. Use an object root with typed properties, an explicit list of required properties, and `additionalProperties: false`. The runner parses and validates the model's answer before it can reach the next block.

## Data and access

By default the server binds to 127.0.0.1. For private remote access use a tunnel or a TLS reverse proxy. The browser sends an owner bearer key with API requests. Anyone with the key has workspace access. Locking the studio clears the key from the current tab. To revoke current keys, stop the server, replace the contents of `owner.key` with a new random secret of at least 32 characters, and restart.

SQLite stores drafts, versioned graphs, input values, model outputs, decisions, and run receipts. Treat these as project data. Model prompts and selected skill text go to the model endpoint chosen for the step. Services receive only the reviewed payload. Register TLS endpoints for credentials crossing machines. Configure access, retention, and backups to match the data you process.

Use `--workspace PATH` after any CLI command to select the data directory. Run only one server per workspace on a local filesystem that supports SQLite and advisory file locks. To back up, stop the process and copy the entire workspace directory (or the named volume) to private storage. Restore it as a directory or volume and supply its credential environment variables before starting. The configuration stores environment variable names. Store their secret values separately.

## Common setup issues

- **No drafting provider**. Configure a named profile and restart Penstock. Under Connections, check that the name appears.
- **Endpoint error**. Confirm the URL includes the full API path, the server can reach it, and the model ID exists there. Use the provider protocol that matches the endpoint's response format.
- **Service delivery**. Review the pending value in the receipt. A failed or interrupted live POST can have an unknown outcome. Check the receiving service before starting another run.
- **A selected connection or skill changed**. Save a new version of the proposal and test it with the new configuration.
- **Container configuration**. Localhost inside a container refers to that container. Use a network address visible to the service and recreate the container after changing credential environment variables.
