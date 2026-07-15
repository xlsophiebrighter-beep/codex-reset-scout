# Security policy

## Supported versions

Security fixes are applied to the latest release and the `main` branch.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for this repository.
Do not open a public issue containing tokens, `auth.json`, session logs, account
IDs, webhook URLs, or screenshots with personal information.

## Credential handling guarantees

- Authentication material is read only for the explicit experimental credit
  command.
- Tokens and raw API responses are never printed or written to Scout state.
- Local JSONL parsing extracts rate-limit fields only.
- Webhook payloads contain normalized alerts and quota percentages, not auth data.

The reset-credit endpoint is a private client endpoint, not a documented public
API. Treat failures or schema changes as expected and report them without sharing
raw responses.
