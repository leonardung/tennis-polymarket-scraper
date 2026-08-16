# If the API can't be reached

Symptom:

```
cannot reach the Polymarket API: [Errno -2] Name or service not known
```

## Why

Many ISP resolvers answer `NXDOMAIN` for `polymarket.com`, so the name fails to
resolve while the rest of the internet works normally. Confirm which side is broken:

```bash
getent hosts gamma-api.polymarket.com          # your resolver — returns nothing
nslookup gamma-api.polymarket.com 1.1.1.1      # public resolver — returns addresses
```

If the second works and the first doesn't, it's the resolver, not the network. Only
the lookup is filtered: once you have an address, the API answers normally, so no
proxy or tunnel is involved.

## What the tool does about it

Nothing needs doing — this is handled automatically. When the system resolver can't
answer, the hostnames are looked up over DNS-over-HTTPS (Cloudflare, falling back to
Google) and pinned for the life of the process:

```
resolving gamma-api.polymarket.com via DNS-over-HTTPS -> 104.18.34.205
```

The DoH endpoints are addressed by IP literal (`1.1.1.1`, `8.8.8.8`), whose
certificates carry those IPs in their SANs. Bootstrapping therefore needs no working
DNS at all, and certificate verification stays on throughout.

| `--dns` | Behaviour |
|---|---|
| `auto` (default) | use DoH only when the system resolver fails |
| `always` | always use DoH |
| `never` | never use DoH; fail if the system resolver can't answer |

On a machine with working DNS, `auto` never activates.

## Fixing it system-wide instead

Optional, and needs sudo. On Ubuntu with `systemd-resolved`, edit
`/etc/systemd/resolved.conf`:

```ini
[Resolve]
DNS=1.1.1.1 8.8.8.8
Domains=~.
DNSOverTLS=opportunistic
```

```bash
sudo systemctl restart systemd-resolved
getent hosts gamma-api.polymarket.com   # should now return an address
```

`Domains=~.` is the line that matters. Without it the per-link DNS handed out by
DHCP still wins and nothing changes.
