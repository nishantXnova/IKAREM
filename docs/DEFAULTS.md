# Defaults, stated plainly

Every default below is a choice with a reason and an override. Nothing
here fails silently: the strict option is always one argument away.

## Validation: unknown keys are ignored

`Schema` drops keys it doesn't declare — unless `extra="forbid"`, which
turns them into 400s. The lenient default keeps old clients working when
you add fields; the strict one belongs on every write path, where an
unknown key is either an attacker probing or a client about to lose data
silently. Rule of thumb: `forbid` on POST/PUT/PATCH, ignore on reads.

## Cookies: HttpOnly always, Secure opt-in

Session cookies are `HttpOnly` with `SameSite=Lax` out of the box.
`Secure` is opt-in via `SessionMiddleware(secure=True)` — turn it on the
moment you serve HTTPS, since without it cookies travel over plain HTTP.
`HttpOnly` is not configurable because there is no legitimate reason to
read a session cookie from JavaScript.

## No response filtering: return exactly what the client should see

There is no `response_model` stripping unknown fields. Whatever the
handler returns is what goes on the wire — so project before returning
(build the response dict explicitly) rather than dumping ORM rows whole.
A leaked column is a handler bug, and it will be your bug, visibly, in
your code — not a framework filter quietly saving you sometimes.

## Rate limiting: X-Forwarded-For is trusted when present

The limiter keys the first IP in `X-Forwarded-For`; with no proxy header
it keys the socket peer, so direct visitors don't share one bucket.
Consequence for proxy deployments: your proxy must overwrite (not
append to) the header, or clients can rotate identities by sending
their own. 429s carry `Retry-After` plus `X-RateLimit-*` headers;
successful responses carry the quota headers too.

## Secrets: defaults refuse to boot

Authenticated routes with `auth_secret`/`session_secret` still at
defaults (or missing) fail at startup, not on first request. Pass real
secrets via config or `IKAREM_AUTH_SECRET` / `IKAREM_SESSION_SECRET`.

## Auth limits, honestly

JWT is HS256 only: no refresh rotation, no server-side revocation list.
Short `expires_in` bounds the exposure; instant revocation needs a
token-blocklist design or the planned `ikarem-oauth` extension (see
`docs/ECOSYSTEM.md`). Scope the project accordingly: service-to-service
and single-app auth are covered; multi-tenant SSO is future work.
