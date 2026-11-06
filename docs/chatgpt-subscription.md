# ChatGPT subscription

In station management, choose **ChatGPT订阅** and explicitly choose its connection mode. For either Codex mode, click **绑定 Codex 账号**, finish the official device-code login, then save the station. Each station stores its own account binding, so multiple Codex accounts are added by creating one station per account. Replacing a binding stays staged until the station is saved; cancelling the editor leaves the current station in place. Read the model catalog after saving a mode or account change. All modes use `store: false`, require complete conversation input, and treat a request as successful only after `response.completed`. This provider sends no background generation probes or scheduled IQ tests.

| Mode | Use |
| --- | --- |
| Subscription-sharing interface | The documented Sign in with ChatGPT preview; uses its separate authorization and model catalog. |
| Codex protocol relay | For local personal use with Responses clients that need tools and real-time streaming; uses the profile's separate official Codex login. |
| Official Codex CLI text mode | Runs the installed CLI for text and manual IQ tests; client tools and multimodal input are not supported. |

## Codex protocol relay

Select **Codex 协议转发 · 支持客户端工具** and bind its Codex account in the station editor. The same native account authorization powers inference and usage; there is no second login just to read quota. Save the station to apply the new binding and discover its models. Clients continue to use the gateway's existing `/v1/responses` URL and gateway key.

This is a native-protocol compatibility adapter, not a subprocess generation task. It sends the original Responses input items, client function/custom tools, tool results, structured-output format and reasoning settings to the fixed Codex Responses endpoint. Tools execute in the client, never in the gateway. Real client session identifiers are preserved; client credentials, cookies, account IDs and forwarded IP headers are not sent upstream. Full history must include returned output items and matching tool `call_id` values; see the [official Responses continuation guidance](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling#continue-after-client-owned-function-calls).

The official CLI owns token refresh in this profile's protected account directory. The relay does not reuse subscription-sharing tokens or follow upstream redirects. It has no CLI-generated system context or local tool execution. Each account is attempted at most once per client request; a disconnected client closes the upstream stream. Changing the bound account replaces its transport.

The native Codex route is separate from the documented subscription-sharing preview and can change with upstream clients. Persistent response IDs and background jobs are rejected; output-budget and sampling hints unsupported by this route are omitted. Keep complete history on the client. Model capability fields are forwarded for upstream validation; support for every model or tool type is not implied. A subscription-sharing restriction is not evidence that Codex itself is unavailable, and switching paths is always explicit.

## Subscription-sharing interface

This mode uses OpenAI's Sign in with ChatGPT preview and its public Responses API. Persistent response IDs, background jobs, and unsupported fields or tools are rejected. Its authorization, model catalog and access limits are independent of the native Codex modes.

## Optional official Codex CLI mode

For local personal use, an authorized station can explicitly select **Official Codex CLI · local text mode**. Bind its Codex account in the station editor, save, and refresh models. The native model list can differ from the subscription-sharing catalog. Update the trusted CLI when its model catalog is outdated. Switching modes is explicit; requests never silently change authentication or billing paths.

This adapter runs the installed official CLI with its own protected account directory, temporary read-only working directory, no saved conversation, and shell, plugins, browser, apps and multi-agent tools disabled. It accepts complete text history through `/v1/responses`, supports reasoning effort and manual IQ tests, and returns Responses events by completed message. It currently rejects client tool calls, images/files, persistent response IDs and structured-output contracts rather than silently dropping them. It is **not a fully compatible replacement for a coding client's raw Responses endpoint**.

In a pool with other compatible channels, normal routing can still send a tool request to one of those channels; this does not mean the CLI supports tools. The original tool definitions and required tool choice are preserved. If only the CLI is eligible, unsupported requests fail locally with HTTP 400 before login or generation. Check the request log's actual channel when validating an integration.

The CLI adds its own context, so even a short prompt has more input tokens than a direct API request. No background generation is enabled. Native inference and metadata operations are serialized per account to avoid concurrent refresh-token rotation. Cancellation terminates the child process, the first CLI-reported error stops the run, and a missing completion is a failure. The gateway sends no additional retry request; the official CLI may perform internal transport handling before it reports an error.

The [official app-server documentation](https://learn.chatgpt.com/docs/app-server#auth-endpoints) permits continued app-server authentication for local or open-source applications and recommends Sign in with ChatGPT; it does not permit using native app-server authentication for commercial or hosted services. Keep this distinction when deploying or extending this personal gateway.

The official repository also includes a [Responses API proxy](https://github.com/openai/codex/tree/main/codex-rs/responses-api-proxy). It forwards API-key or GitHub Actions OIDC-authenticated API requests and keeps credentials in a privileged process. Its documented authentication is separate from ChatGPT subscriptions; adding that proxy does not establish that a subscription-sharing restriction has recovered.

Visual tests are available on demand. The output-token field is hidden for ChatGPT subscriptions and omitted from upstream requests because this route does not support it. Other providers default to 12,000 output tokens, editable without a gateway-imposed maximum. Subscription visual tests send one request, wait up to 120 seconds, and never retry automatically. Automated visual tests and background generation probes are disabled for this provider.

Within a client request, each ChatGPT account is attempted at most once, including across retry rounds and parameter fallback. Rate limits respect the upstream waiting period. These controls avoid unnecessary duplicate traffic; they cannot guarantee an upstream account decision or model quality.

## Usage and reset credits

Usage and earned rate-limit reset credits are read from the account bound to that station's Codex mode. In subscription-sharing mode, this optional usage login remains separate from model access. Reading limits does not generate a model response. The page displays upstream windows and any available reset credits; if OpenAI reports only a count, ModelHarbor can ask Codex to select the next available credit.

The channel table uses compact quota progress bars, with reset times and credits in a detail dialog. Connected profiles refresh metadata every 15 minutes; failed reads back off and retain cached data across restarts. Only windows returned by the account are shown.

Quota reads wait at most 3 seconds for an account already in use and have a 45-second read deadline. Failed reads retain the last successful values and allow another manual attempt after 60 seconds. Connection failures are distinct from expired authorization; network timeouts do not require a new device-code login. Dead CLI connections are replaced on the next operation. The dialog releases its loading state as soon as its own request finishes and also limits a disconnected browser request to 55 seconds.

Native Codex `usage_limit_reached` errors retain the upstream reset time, `Retry-After` and limit bucket when provided, including errors inside an HTTP 200 stream. The channel waits until that time (at least 60 seconds). Without a reset, waits start at 15 minutes and double on repeated usage failures, up to 6 hours. Fresh matching quota metadata can extend a wait but spare capacity alone never clears a failure. After the wait, one real client request or a manually submitted IQ test can validate recovery; only complete success clears the restriction. Channels sharing the same bound account cannot send another request while that recovery trial is in progress.

A cancelled or incomplete trial releases its slot and keeps a short 60-second guard; it does not increase the usage-limit backoff. A new upstream usage rejection starts a new wait, and explicit upstream reset times and rate limits still apply. The test dialog shows the restriction, a live countdown and whether a submission was definitely rejected or its outcome is unknown. There are no background generation retries. Waiting and the last error survive restarts, including correction of older interrupted trials that unnecessarily renewed a long wait. Subscription-sharing restrictions remain separate. Different models or quota buckets can still be restricted when the displayed main windows have capacity.

These windows come from the **Codex account interface**. They do not establish that the separate Sign in with ChatGPT subscription-sharing endpoint is available. If that endpoint returns `subscription_sharing_usage_limit_exceeded`, ModelHarbor pauses the channel without asserting that the entire plan is exhausted or inventing a reset time. Open **View restriction → Manage official usage** to review app limits. Repeated OAuth sign-ins are not a recovery strategy for this error.

**Manually verify once** sends one short generation request after confirmation, honors upstream rate limits and a minimum 60-second retry interval, and keeps the last IQ-test preview. Only a complete successful response clears protection; refreshing usage, reading models, disabling a channel, and restarting the service do not. There are no automatic subscription recovery requests. If the official settings show capacity but the endpoint still refuses, this can require an upstream fix; see [OpenAI Codex issue #51043](https://github.com/openai/codex/issues/51043) and the [official recovery guidance](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery).

Opening usage or refreshing it reuses the existing authorization. For Codex relay and CLI stations, reauthorization refreshes the same account credential used for model calls; switch accounts from the station editor. In subscription-sharing mode, **Reauthorize usage** changes only the optional usage login. The dialog identifies the linked account, shows loading feedback, and lets you copy the verification code before opening the official login page.

Using a reset credit always requires a browser confirmation. The redemption uses an idempotency key, and ModelHarbor refreshes account limits afterward. The upstream decides whether a reset is eligible and returns the outcome; the gateway does not manufacture credits or quota.

Native Codex binding and usage require an OpenAI-signed `codex.exe` installed beneath `Program Files`, with its path set in `MODELHARBOR_CODEX_PATH`. For an NSSM Windows service, an administrator can install the signed executable found with the Codex CLI and restart the gateway:

```powershell
$codex = Get-ChildItem "$env:APPDATA\npm\node_modules\@openai\codex\node_modules\@openai" -Filter codex.exe -Recurse | Select-Object -First 1
./tools/Install-CodexBridge.ps1 -SourcePath $codex.FullName
Restart-Service ModelHarbor
```

The installer verifies the publisher, copies only the executable into an ACL-protected machine directory, and sets the service environment variable. It does not copy or read the interactive user's login files. Each Codex station's device-code binding is stored under its protected `data/chatgpt/codex-<profile>` directory and powers both inference and account usage. In subscription-sharing mode, the app-server runs only when the user asks to connect the optional usage panel and stores that separate login under the same protected data root. If the trusted CLI is unavailable or the account does not expose reset credits, inference availability and the usage panel report their limitations independently.

If the deployment host needs an HTTP(S) proxy to read Codex account metadata, set `MODELHARBOR_CODEX_PROXY` to the existing proxy's URL, including its port, in the gateway service environment and restart the service. This optional setting applies only to the official Codex account app-server (authorization, usage and model metadata). It does not override networking for subscription inference, Codex protocol forwarding, CLI text generation or other stations, and does not enable background generation probes. Without it, the account app-server inherits the service's normal proxy environment. A Windows service does not inherit the interactive desktop user's browser proxy settings. `workspace routing discovery timed out` indicates that Codex could not complete its workspace connection; check the deployment host's connectivity before reauthorizing. Proxy addresses and credentials belong in local service configuration, never in the repository.

Official references: [Sign in with ChatGPT models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference), [preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations), and [Codex App Server earned rate-limit resets](https://learn.chatgpt.com/docs/app-server#8-earned-rate-limit-resets-chatgpt).
