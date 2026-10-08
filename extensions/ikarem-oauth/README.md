# ikarem-oauth — refresh tokens + social login for IKAREM

Core `ikarem` mints short-lived access JWTs and stops there. This is the
long-lived half: opaque refresh tokens (rotation, reuse detection,
revocation) and OAuth2 code-flow login (Google/GitHub presets, any
provider via URLs), all stdlib-only (provider HTTP over `urllib`).

```bash
pip install ./extensions/ikarem-oauth
```

```python
from ikarem.db import DatabasePlugin
from ikarem_oauth import OAuthPlugin, github_provider


async def find_or_create(provider, info):  # your users table, your rules
    return f"{provider}:{info['id']}"


app.register(DatabasePlugin("sqlite:///app.db"))
app.register(
    OAuthPlugin(
        auth_secret="32-byte-secret",
        provider=github_provider("CLIENT_ID", "CLIENT_SECRET"),
        on_user=find_or_create,
    )
)
```

Routes: `GET /oauth/login?redirect_uri=` → 302 provider;
`GET /oauth/callback?code=&state=` → `{access_token, refresh_token}`;
`POST /oauth/token {grant_type: refresh_token, ...}` → rotated pair
(replayed rotated token = theft: whole chain revoked);
`POST /oauth/revoke {refresh_token}` → logout.
Password logins stay in your app — call `store.issue(sub, secret)`
(`app.state_oauth`) after your own password check.

Security properties: refresh tokens stored hashed (sha256, like API
keys); login `state` HMAC-signed + 10-min TTL (CSRF); provider transport
injectable (`http_post=`/`http_get=`) so tests run network-free.

## Removal path

Uninstall and you keep core JWT (`create_token`/`verify_token`,
`BearerAuth`, password hashing). You lose refresh rotation and social
login — re-add with a `sessions` dict only if you accept forever-tokens
(you shouldn't; that's why this exists).
