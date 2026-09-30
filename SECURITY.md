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
