# Releasing

Maintainer notes. The npm account (`beiningcheng`) enforces **2FA with a security key only** —
TOTP cannot be configured on it any more, so every publish goes through one browser approval.

## Publish a new version

```bash
# 1. bump the version and add a CHANGELOG entry
npm version patch            # or minor / major, then edit CHANGELOG.md

# 2. publish — this needs a real TTY, so run it in a terminal, not in a pipe
HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897 \
NODE_OPTIONS=--dns-result-order=ipv4first \
npm publish --cache /tmp/npmcache
```

When npm prints

```
Authenticate your account at:
https://www.npmjs.com/auth/cli/<uuid>
Press ENTER to open in the browser...
```

press **Enter**, then in the opened page click **Use security key** and complete the system
prompt. The CLI publishes as soon as the browser flow succeeds.

Notes that cost time to rediscover:

- The registry is IPv6-only in its DNS answer and this machine has no working IPv6 route, so
  `NODE_OPTIONS=--dns-result-order=ipv4first` plus the local proxy is mandatory.
- `npm publish` in a non-TTY (piped) shell prints the auth URL as `***` and can never finish.
- A granular access token without *Bypass 2FA* gets `403 ... Two-factor authentication or
  granular access token with bypass 2fa enabled is required to publish packages`.
- The registry briefly exposes a `0.0.0-stage` placeholder while a version is being processed;
  it is not `latest` and can be ignored.

## GitHub release

```bash
git push origin main
git tag -a v<version> -m "v<version>"
git push <remote-with-token> v<version>
npm pack --cache /tmp/npmcache
# then upload the tarball as a release asset through the API
```

## Marketplace listing

`awesome-dsh-plugin` takes one YAML file per plugin at
`data/plugins/<owner>__<repo>.yml` (see the repository's contributing guide). Gates:

- `package.json` must declare `dsh.bundle`
- the repository must be **at least 24 hours old**
- the repository must carry the `dsh-plugin` topic
