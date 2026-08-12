# Threat model

## Assets

- The per-location Elastic IP identity registered as a Cisco Secure Access Network.
- ThousandEyes Account Group Installation Token.
- PAC URL and PAC contents.
- AWS credentials, SSH keys, instance role credentials, and customer identifiers.
- Integrity of bootstrap code and the registered agent.

## Trust boundaries and threats

| Boundary | Primary threats | Controls |
|---|---|---|
| Operator to AWS APIs | terminal leakage, accidental persistence | hidden input, SDK TLS, mode `0600` ARN-only config |
| S3 bootstrap to EC2 | asset replacement, public disclosure | private bucket, blocked public access, IAM object ARN, SHA-256 content address |
| EC2 metadata | SSRF credential theft | IMDSv2 required, hop limit 1 |
| Elastic IP identity | reuse/loss of the registered-network trust anchor | tag-matched reuse, never released by tooling, allocated only for selected regions |
| EC2 to PAC endpoint | TLS downgrade, captive/login page, oversized response, unregistered source IP | TLS verification, no HTTP redirect, content limit/type/syntax checks, retried reachability check with an actionable failure message |
| EC2 to ThousandEyes | premature/untrusted registration, token exposure | fail-closed gate, disabled service on failure, default no inbound access |
| Logs and diagnostics | secret/PAC disclosure | coarse status, URL redaction, no shell tracing or secret command output |

## Residual risks

- The ThousandEyes installer token is briefly visible to root through the process table because the supported installer accepts it as a positional argument.
- A compromised root user or instance role can retrieve deployment secrets.
- Cisco Secure Access Network registration is a manual GUI step outside this tool's control; a location's EIP can be deployed before it is registered, causing the proxy-reachability check to fail until an operator completes registration.
- Third-party public-IP services are no longer queried by the bootstrap; the instance's Elastic IP is authoritative and known in advance.
- Tenant-specific Cisco Secure Access policy and identity limits require integration testing.

Rotate an installation token after suspected disclosure, terminate the affected host, remove/re-register the agent, and inspect CloudTrail access events. If a location's Elastic IP is suspected compromised, deregister it in Cisco Secure Access and manually release it in AWS — this tool will never do so automatically.
