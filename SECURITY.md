# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.x | Yes — patch releases for vulnerabilities |
| 0.x | No — pre-release proving ground; upgrade to 1.x |

## Reporting a vulnerability

Do **not** open a public issue. Report privately via
[GitHub private vulnerability reporting](https://github.com/nishantXnova/IKAREM/security/advisories/new)
so a fix can ship before details go public.

Include: affected version, minimal reproduction (route + request), and the
impact you see (auth bypass, data leak, DoS, …). Proof-of-concept code
beats prose.

## What happens next

Valid reports get a fix plus a regression test, released as a patch with a
`### Security` entry in `CHANGELOG.md`. Credit in the release notes unless
you ask to stay anonymous. Anything touching the zero-dep core stays
stdlib-only — even security fixes don't add required dependencies.

## Name collision: malicious "@ikarem/telemetry" npm package

A malicious npm package was published as "@ikarem/telemetry" and shares the IKAREM name. It is not affiliated with this project in any way.

IKAREM is a Python framework. We publish no npm packages. The "@ikarem/telemetry" package is unrelated to this repository, its maintainer, and its users.

The package has been identified as malicious by OpenSSF Package Analysis and is tracked by OSV as MAL-2025-192569 / GHSA-5q62-g32j-g4hp.

If a security scanner flags "@ikarem/telemetry", this does not indicate a vulnerability in the IKAREM Python project. It refers to the unrelated npm package.

If you have installed or executed "@ikarem/telemetry", follow the remediation guidance in the corresponding security advisory rather than treating it as an IKAREM dependency.

Official IKAREM distribution channels

IKAREM is distributed only through:

- PyPI: "pip install ikarem"
- GitHub: "nishantXnova/IKAREM"

We do not publish or maintain "@ikarem/*" npm packages.