# Model discovery

Codex Assist retrieves the chat-model list using its own ChatGPT/Codex sign-in.
It filters hidden models, removes duplicates, and follows the backend's priority
order. It does not invent model IDs, add `-pro` variants, or insert a hardcoded
model into a successful discovery result. New models can appear without updating
the integration.

During first setup, sign in before choosing a model. The first model in the
account’s returned priority order is preselected. Existing configured models
are not migrated automatically.

The list refreshes when you open options and every six hours while the integration
is loaded. Repeated opens within 30 seconds reuse the cache. Failed requests have
a 60-second retry backoff. Discovery does not run on every voice request or block
integration startup.

| Result | What the picker shows |
| --- | --- |
| Successful discovery | Only visible models returned for the account. |
| Recent cached result | The last successful list, labeled as cached. |
| Failed refresh with a cache | The cached list with an outdated-list warning. |
| Failed discovery without a cache | A small list of suggestions, explicitly unverified for the account. |
| Successful empty list | No suggested models; new setup can retry later. |
| Saved model absent from the list | The saved choice labeled `(saved; not currently listed)`, without replacing it. |

If setup says your account returned no visible models, try again later or restart setup with another account.

The cache is isolated per integration entry and held only in memory. Restarting
Home Assistant discards it. Signing in again clears it, including pending results
from the old sign-in. Credentials continue to be owned and refreshed by the
integration; discovery does not borrow tokens from other tools.

A listed model can still reject a request or lack a particular feature. If a saved
model stops working, open options and choose a replacement. Codex Assist does not
silently select another model or automatically replay a device-control request.
The normal dropdown does not accept arbitrary model IDs. Image-generation models
remain separate from chat-model discovery.

OpenAI retired `gpt-5.4` and `gpt-5.4-mini` from Codex with ChatGPT sign-in on
August 31, 2026. Its [models guide](https://learn.chatgpt.com/docs/models#deprecated-codex-models)
says to replace `gpt-5.4` with `gpt-6-sol` and `gpt-5.4-mini` with `gpt-6-luna`
when they are available to your plan and client. The retirement does not affect
API-key use. If a retired model is still saved, open options and choose a
replacement from your own list. Codex Assist shows only what your account
returns, so a name in the guide may not appear for you.

The downstream Codex model-discovery endpoint is not a stable public third-party
API contract. Cached results and fallback suggestions allow settings to remain
usable during outages; they do not guarantee account access.
