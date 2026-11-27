# Managed project access

Managed access adds named sign-in and project membership to a Community workspace. The workspace owner key remains an installation-wide credential. Keep it with the operator who maintains the workspace. Give other users access through project invitations.

## Set up the workspace

Initialize and serve Penstock. For a shared installation, terminate HTTPS at a reverse proxy, preserve the public `Host` header, and keep the raw Penstock listener private. Penstock uses the configured origin to check state-changing browser requests. An HTTP origin is supported only on loopback for local development.

1. Open Community Edition over HTTPS or on a loopback address and sign in with the workspace owner key. Choose **Team setup** in the workspace menu.
2. Enter the full HTTPS origin staff will open, with no trailing path. Choose **Local invitations** or **Organization sign-in (OIDC)**. For OIDC, enter the issuer, client ID, and optional name of the secret environment variable. Set that variable in the server environment before saving. The form shows the redirect URI to register with the identity provider.
3. Choose **Save settings for restart**, then restart the Penstock service from your host or container manager using the same configuration and environment. Open the configured URL with your owner key. **Team setup** shows whether invitations are active and links to **Project access**, **Connections**, and the **Inbox** for the selected project.

Team setup is for the workspace owner of a self-hosted installation. It saves the same `managed.json` used by the command line. The browser setup checks local OIDC requirements and credentials availability without contacting the provider. If a required dependency or environment variable is missing at startup, owner access stays available and Team setup shows the startup error. Restore the server dependency or environment variable and restart. Verify organization sign-in with a test invitation after activation.

### Command-line setup

The browser setup and `managed-init` both create the managed configuration once. For an unattended installation, use the command line before starting the service.

```bash
python3 penstock.py init --workspace /srv/penstock
python3 penstock.py managed-init --workspace /srv/penstock --public-origin https://penstock.example.org
python3 penstock.py serve --workspace /srv/penstock
```

For organization sign-in, configure the provider before running `managed-init`. Register an OpenID Connect client that supports authorization code, S256 PKCE, RS256 ID tokens, and the exact redirect URI `https://penstock.example.org/api/auth/oidc/callback`. Public clients use no token-endpoint credential and confidential clients use `client_secret_basic`. Use the provider’s exact issuer value, including any issuer path. For a bare Python installation with OIDC, install the optional dependencies in the environment running Penstock. The Community container already includes them.

```bash
python3 -m pip install -r requirements-managed.txt
python3 penstock.py managed-init --workspace /srv/penstock \
  --public-origin https://penstock.example.org \
  --oidc-issuer https://identity.example.org \
  --oidc-client-id penstock \
  --oidc-client-secret-env PENSTOCK_OIDC_CLIENT_SECRET
```

Set the named variable for the `serve` process using your deployment’s secret store. Omit the last flag for a public client. `managed-init` creates `managed.json` once. To update an existing installation, review that file locally and restart. Users and sessions are in `managed.sqlite3`. Back it up with the workspace databases and use the same access controls. Identity provider tokens are used during sign-in and are never saved in the workspace. Configure MFA or organization sign-in policy at the identity provider.

## Invite and manage project members

Open the studio with the owner key, choose a project, and open **Project access**. Choose an email, a project role, and local or organization sign-in. Create an invitation and deliver the link privately to the intended person. A new invitation for an address supersedes its previous invitation for that project. Each link expires after 48 hours and is consumed once.

For local sign-in, the first invitation sets a password of at least 15 characters. Use the current password for later invitations. For organization sign-in, Penstock matches a verified email claim to the invitation. If the provider omits `email_verified`, supply the exact OIDC `sub` from trusted administrator records when creating the invitation. An existing OIDC identity signs back in by issuer and subject. The same email cannot automatically merge local and organization identities.

| Role | Project access |
| --- | --- |
| Viewer | Read the workspace catalog, run receipts, and project data. |
| Reviewer | View and decide on waiting steps. The receipt records the sign-in identity and project role. |
| Editor | View, save workflow drafts, edit project assets, and start offline previews or tests. |
| Executive | Describe a job, edit its plan, test it, and approve project reviews. IT retains publishing, live-run initiation, and connection management. |
| Operator | Combine editor and reviewer access with publishing and starting live project runs. |
| Admin | Add project invitations and role management, plus administration of provisioned project connections. |
| Workspace owner | Create projects, configure workspace-wide connections and triggers, and authorize registered external connector operations. |

A project admin can update or revoke access from the member list. Authorization checks happen on each request and use the stored project for workflow and run IDs. Signing out ends the current session. Sessions expire after eight hours. Revoking the last membership invalidates that person’s sessions for the workspace. Decisions already recorded remain in the run. Each claimed review decision and its actor are stored before workflow execution resumes.

## Review model and connection permissions

- **Offline preview** uses a temporary project snapshot, simulates registered service steps, and makes no model calls. A step that needs a model stops with an explanation.
- **Model test** keeps the same snapshot and simulated service behavior. Explicit consent allows the selected model to receive the step input and configured instructions. Drafting with a model separately requires consent to send the brief. Project table metadata and published workflow names are sent only when their inclusion is selected.
- **Live run** executes the published version. Managed operators and admins can start local project actions and consent to use configured models. Only the workspace owner can allow an existing workspace-wide registered service route to contact its destination. Reads and writes are separate permissions for that run, and each service operation still waits for review. Provisioned office-reading adapters have project-scoped administrator checks and per-read consent. Only the workspace owner enables outbound notification destinations.

Installations that call the original owner-key APIs can keep using their existing payloads and owner trust model. Browser-based managed sessions require same-origin requests and the session CSRF token on POST. Never share an owner key for routine review or operations.
