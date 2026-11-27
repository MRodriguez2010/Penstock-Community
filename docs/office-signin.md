# Google and Microsoft connection sign-in

Penstock lets a project administrator connect one delegated account per supported reader. A connection serves its project. Choose an account whose mailbox or drive is appropriate for that project. Use a separate account or app registration when projects need different access boundaries.

## Register the OAuth applications

Serve Penstock on its public HTTPS origin and preserve that `Host` at the reverse proxy. Keep the backend listener private. A bare Python deployment needs the dependencies in `requirements-managed.txt`. The Community container includes them.

1. In Google Cloud, enable the Gmail API or Drive API, configure the consent screen, and create a **Web application** OAuth client. Add the exact Penstock redirect URI for each reader you enable. Google Workspace organization settings, publication status, and restricted-scope verification can affect who may authorize the application. [Google web server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)
2. In Microsoft Entra, register a single-tenant web application, add the exact redirect URIs, and grant only the delegated Microsoft Graph permissions for the readers you enable. Set a client secret and follow your tenant’s consent policy. Use the directory tenant UUID and the application client UUID. [Microsoft authorization code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)
3. Set the client secrets as environment variables for the Penstock process. Put the client IDs and the names of their secret variables in `operations.toml` under the intended project ID. Use the exact public Penstock URL users open.

| Reader | Redirect URI for `https://penstock.example.org` | Delegated permission |
| --- | --- | --- |
| Gmail | `https://penstock.example.org/api/office-oauth/callback/google-mail` | `https://www.googleapis.com/auth/gmail.metadata` |
| Google Drive | `https://penstock.example.org/api/office-oauth/callback/google-drive` | `https://www.googleapis.com/auth/drive.metadata.readonly` |
| Outlook | `https://penstock.example.org/api/office-oauth/callback/microsoft-mail` | `Mail.ReadBasic` and `offline_access` |
| OneDrive | `https://penstock.example.org/api/office-oauth/callback/microsoft-files` | `Files.Read` and `offline_access` |

Gmail and Outlook return up to five recent subjects. Google Drive and OneDrive return up to five names. A read of OneDrive uses the minimum delegated Graph permission for listing folder children. Start with only the readers you need. [Gmail method scopes](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list), [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [Outlook permissions](https://learn.microsoft.com/en-us/graph/api/user-list-messages), [OneDrive permissions](https://learn.microsoft.com/en-us/graph/api/driveitem-list-children)

```toml
[projects."<project-uuid>".google-drive]
oauth_origin = "https://penstock.example.org"
oauth_client_id = "<google-web-client-id>.apps.googleusercontent.com"
oauth_client_secret_env = "PENSTOCK_GOOGLE_OAUTH_SECRET"

[projects."<project-uuid>".microsoft-mail]
oauth_origin = "https://penstock.example.org"
oauth_client_id = "<entra-application-client-uuid>"
oauth_client_secret_env = "PENSTOCK_MICROSOFT_OAUTH_SECRET"
oauth_tenant = "<entra-tenant-uuid>"
```

## Connect and verify

Open **Connections** in the project as an administrator. Select the configured account’s sign-in action, review the requested access, and finish at the provider in the same tab. Keep the tab open. If the workspace asks you to sign back in, use the Penstock account that started the connection. Penstock authorizes the recovery against that account, project, browser and connector. The attempt expires after ten minutes.

After connection, choose **Preview a read** and review the live read confirmation. Test workflows use the sample on each connected source step. A live workflow needs explicit read permission and a separate review at the connected source step before it can contact the service.

Access and refresh tokens are encrypted in the workspace database using `.office-oauth.key`. Restrict the key, workspace and backups to the service account. Preserve the key with encrypted backups. Losing it requires reconnecting accounts. Penstock refreshes a grant only when an authorized read needs it. **Disconnect** removes the project’s saved authorization and pending connection attempts. Use the Google or Microsoft account’s consent settings if you also need to remove the app grant at the provider.

## Check a failed sign-in

- Redirect mismatch. Compare the exact authorized HTTPS callback URI and connector ID against the table above.
- Consent declined or unavailable. Ask your Google Workspace or Entra administrator to verify the configured consent policy and the application’s reader permissions.
- The provider asks to reconnect. Confirm the client secret is current and the tenant’s refresh-token policy allows continued access.
- The browser asks to restart. Return to the same tab and project where you started. If ten minutes passed, start a new connection.
