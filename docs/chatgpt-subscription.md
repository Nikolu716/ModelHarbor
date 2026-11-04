# ChatGPT subscription

ModelHarbor can use a ChatGPT Plus or Pro account through OpenAI's Sign in with ChatGPT flow. In station management, choose **ChatGPT订阅**, authorize the intended account, read its visible model catalog, then save and enable the station. The gateway uses the account-specific model slug and the public Responses API. It sends `store: false` and `stream: true`, and treats a request as successful only after `response.completed`. It does not run background generation probes.

This subscription route is a preview with limits. It supports Responses requests only; the client must send the full conversation input. Persistent response IDs, background jobs, and unsupported Responses fields or tools are rejected. Supported models and access depend on the signed-in account and its workspace.

Visual tests are available on demand. The output-token field is hidden for ChatGPT subscriptions and omitted from upstream requests because this route does not support it. Other providers default to 12,000 output tokens, editable without a gateway-imposed maximum. Subscription visual tests send one request, wait up to 120 seconds, and never retry automatically. Automated visual tests and background generation probes are disabled for this provider.

Within a client request, each ChatGPT account is attempted at most once, including across retry rounds and parameter fallback. Rate limits respect the upstream waiting period. These controls avoid unnecessary duplicate traffic; they cannot guarantee an upstream account decision or model quality.

## Usage and reset credits

Usage and earned rate-limit reset credits are optional and separate from model access. Select **Connect usage and reset credits** and complete the official Codex device-code login with the same ChatGPT account. Reading limits does not generate a model response. The page displays upstream windows and any available reset credits; if OpenAI reports only a count, ModelHarbor can ask Codex to select the next available credit.

The channel table uses compact quota progress bars, with reset times and credits in a detail dialog. Connected profiles refresh metadata every 15 minutes; failed reads back off and retain cached data across restarts. Only windows returned by the account are shown.

Using a reset credit always requires a browser confirmation. The redemption uses an idempotency key, and ModelHarbor refreshes account limits afterward. The upstream decides whether a reset is eligible and returns the outcome; the gateway does not manufacture credits or quota.

The optional usage bridge requires an OpenAI-signed `codex.exe` installed beneath `Program Files`, with its path set in `MODELHARBOR_CODEX_PATH`. For an NSSM Windows service, an administrator can install the signed executable found with the Codex CLI and restart the gateway:

```powershell
$codex = Get-ChildItem "$env:APPDATA\npm\node_modules\@openai\codex\node_modules\@openai" -Filter codex.exe -Recurse | Select-Object -First 1
./tools/Install-CodexBridge.ps1 -SourcePath $codex.FullName
Restart-Service ModelHarbor
```

The installer verifies the publisher, copies only the executable into an ACL-protected machine directory, and sets the service environment variable. It does not copy or read the interactive user's login files. The app-server runs only after the user asks to connect usage and stores its separate login under the protected `data/chatgpt/codex-<profile>` directory. If the trusted CLI is unavailable or the account does not expose reset credits, model access remains available and the optional panel reports that limitation.

Official references: [Sign in with ChatGPT models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference), [preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations), and [Codex App Server earned rate-limit resets](https://learn.chatgpt.com/docs/app-server#8-earned-rate-limit-resets-chatgpt).
