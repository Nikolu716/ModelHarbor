# Security Policy

## Supported versions

Security fixes target the latest commit on the default branch. Older snapshots
may not receive updates.

## Reporting a vulnerability

Please do not open a public issue for a credential leak, authentication bypass,
request proxying issue, or other security-sensitive report. Use a private
GitHub security advisory for this repository when available. Include:

- a short description and impact;
- the affected commit or version;
- reproducible steps that do not expose real credentials;
- a suggested mitigation, if known.

Do not include API keys, bearer tokens, webhook URLs, personal data, or private
provider responses in a report.

## Deployment guidance

- Keep `config.yaml` and the entire `data/` directory private. They can contain
  upstream credentials, request metadata, statistics, and runtime state.
- Start from `config.example.yaml`; generate unique high-entropy values for
  `gateway.api_keys` and `gateway.admin_key`.
- Keep the listener on `127.0.0.1` unless remote access is required. For remote
  access, restrict the firewall, use HTTPS through a trusted reverse proxy, and
  require authentication on both API and admin routes.
- Rotate every credential if it was placed in a public repository, log, screen
  capture, unencrypted archive, or untrusted HTTP request.
- Review the upstream provider terms and authorization before forwarding any
  request through the gateway.

## Intended use and isolation

ModelHarbor is intended for personal use and small groups of trusted users.
Account switching selects the globally active configuration; it is not a
multi-tenant authorization system. An admin-key holder can manage all accounts.
Strict permission boundaries and isolated test execution require further
development and review before use with untrusted tenants or public users.

IP access rules use the TCP peer address, not forwarded headers. A local reverse
proxy can make remote requests appear local, including to deployment-only service
settings. Enforce source restrictions and admin access at the proxy itself.
Preview restrictions do not replace a hardened execution sandbox.
