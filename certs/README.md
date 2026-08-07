# Certificate creation and usage

## Usage

In `config.yaml` under `server`:

```yaml
server:
  certfile: localhost.apache.org+3.pem
  keyfile: localhost.apache.org+3-key.pem
```

Paths are relative to this `certs/` directory, or absolute. Leave both blank
for plain HTTP (e.g. production behind a reverse proxy).

## Local OAuth (development)

Apache OAuth and asfquart session cookies expect a trusted **HTTPS** origin.
Use [mkcert](https://github.com/FiloSottile/mkcert) for a local CA and a cert
that includes **`localhost.apache.org`**.

On Ubuntu:

```sh
sudo apt install mkcert libnss3-tools
mkcert -install
cd certs
mkcert localhost.apache.org localhost 127.0.0.1 ::1
```

Ensure `localhost.apache.org` resolves to this machine (often already true on
ASF developer hosts, or add to `/etc/hosts`):

```
127.0.0.1 localhost.apache.org
```

Then open `https://localhost.apache.org:<port>/` (port from `config.yaml`).

## Browser trust

If the browser still warns, import the **mkcert root CA** (not only the site
cert) into the browser trust store. On Chrome: Settings → Privacy and security
→ Security → Manage certificates → Authorities → Import the mkcert CA from
`mkcert -CAROOT`.
