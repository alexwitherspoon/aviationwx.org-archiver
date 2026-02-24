# Security Policy

## Supported Versions

We release security updates for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| ≥ 1.0   | :white_check_mark: |
| < 1.0   | :x:                |

Pre-1.0 releases are not covered by this policy. We will release 1.0 when it is ready.

## Reporting a Vulnerability

If you discover a security vulnerability in the AviationWX.org Archiver, please report it responsibly.

**Do not** open a public GitHub issue for security vulnerabilities.

### How to Report

1. **Preferred**: Use [GitHub's private vulnerability reporting](https://github.com/alexwitherspoon/aviationwx.org-archiver/security/advisories/new) if enabled.

2. **Alternatively**: Email the maintainers with details. You can reach us via [AviationWX.org](https://aviationwx.org) or the repository owner's GitHub profile.

3. **Include** in your report:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)
   - Your preference for acknowledgment in the advisory

4. **Allow** up to 90 days for a fix before public disclosure, unless we agree otherwise.

### What to Expect

- **Acknowledgment**: We will confirm receipt within 7 days.
- **Assessment**: We will triage and assess the report.
- **Updates**: We will keep you informed of progress and any planned fix.
- **Credit**: We will credit you in the security advisory (unless you prefer to remain anonymous).

### Scope

This project is intended for **local or private deployment**. The web GUI has no built-in authentication and should not be exposed to the public internet. Security issues we consider in scope include:

- Remote code execution
- Path traversal or arbitrary file access
- API key or credential exposure
- Injection (e.g. XSS, command injection)
- Denial of service affecting the archiver or host

Out of scope: issues that require the web GUI to be exposed to untrusted networks without a reverse proxy or firewall.

## Security Update Policy

- Security fixes are released as patch versions (e.g. 1.0.1 → 1.0.2).
- We aim to release fixes within 30 days of confirmation for high-severity issues.
- GitHub Security Advisories will be published when fixes are released.
