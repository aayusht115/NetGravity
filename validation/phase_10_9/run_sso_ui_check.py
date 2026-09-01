"""
Browser check: does the SSO button appear only when a provider is configured,
and does it lead to the provider?

Two runs against the same app, one with OIDC unset and one with it pointed at a
loopback fake provider. The point of the first run is that a deployment with no
provider shows NOTHING — a button that leads to an error page teaches a user the
application is broken rather than that the feature is off.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from wsgiref.simple_server import make_server

sys.path.insert(0, r"D:\Case Comp\Kearney\netgravity")

APP_PORT = 5098
IDP_PORT = 5097
ISSUER = f"http://127.0.0.1:{IDP_PORT}"
CLIENT_ID = "netgravity-ui-check"

results = []


def check(cid, description, ok, detail=""):
    results.append((cid, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {cid:6} {description}"
          + (f" - {detail}" if detail else ""), flush=True)


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def start_fake_idp(jwk):
    """Discovery and JWKS only; the flow itself is covered by the test suite."""
    discovery = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = None
            if self.path.startswith("/.well-known/openid-configuration"):
                body = discovery
            elif self.path.startswith("/jwks"):
                body = {"keys": [jwk]}
            elif self.path.startswith("/authorize"):
                # Where a real provider would show a login form.
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>FAKE PROVIDER LOGIN</body></html>")
                return
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", IDP_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    import os
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    jwk = {
        "kty": "RSA", "kid": "ui-key-1", "use": "sig", "alg": "RS256",
        "n": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }
    start_fake_idp(jwk)

    os.environ["NETGRAVITY_SEED_DEMO"] = "0"
    from app.backend.app import app
    from playwright.sync_api import sync_playwright

    server = make_server("127.0.0.1", APP_PORT, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(1.0)
    base = f"http://127.0.0.1:{APP_PORT}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text)
                if m.type == "error" and "Content Security Policy" in m.text else None)

        # ---- 1. no provider configured -------------------------------
        for key_name in ("NETGRAVITY_OIDC_ISSUER", "NETGRAVITY_OIDC_CLIENT_ID",
                         "NETGRAVITY_OIDC_REDIRECT_URI"):
            os.environ.pop(key_name, None)
        page.goto(base, wait_until="networkidle")
        page.wait_for_timeout(1500)
        state = page.evaluate("""() => {
            const host = document.getElementById('auth-sso-option');
            return {present: !!host, hidden: host ? host.hidden : null,
                    html: host ? host.innerHTML.length : -1};
        }""")
        check("S-01", "with no provider, the SSO block stays hidden and empty",
              state["present"] and state["hidden"] and state["html"] == 0,
              json.dumps(state))

        api = page.evaluate("""async () => {
            const r = await fetch('/api/auth/oidc/providers');
            return {status: r.status, body: await r.json()};
        }""")
        check("S-02", "the providers endpoint says SSO is off, with a reason",
              api["status"] == 200 and api["body"]["enabled"] is False
              and "NETGRAVITY_OIDC_ISSUER" in api["body"]["reason"],
              json.dumps(api["body"])[:160])

        # ---- 2. provider configured ----------------------------------
        os.environ["NETGRAVITY_OIDC_ISSUER"] = ISSUER
        os.environ["NETGRAVITY_OIDC_CLIENT_ID"] = CLIENT_ID
        os.environ["NETGRAVITY_OIDC_REDIRECT_URI"] = \
            f"{base}/api/auth/oidc/callback"
        os.environ["NETGRAVITY_OIDC_PROVIDER_NAME"] = "Acme Directory"
        os.environ["NETGRAVITY_DISABLE_RATE_LIMIT"] = "1"

        page.goto(base, wait_until="networkidle")
        page.wait_for_timeout(1800)
        state = page.evaluate("""() => {
            const host = document.getElementById('auth-sso-option');
            const btn = document.getElementById('auth-sso-start');
            return {
                hidden: host ? host.hidden : null,
                text: btn ? btn.innerText.trim() : '',
                href: btn ? btn.getAttribute('href') : '',
                visible: btn ? btn.getBoundingClientRect().height > 0 : false,
            };
        }""")
        check("S-03", "with a provider, the button appears and names it",
              state["hidden"] is False and "Acme Directory" in state["text"],
              json.dumps(state))
        check("S-04", "the button is actually visible on the page",
              bool(state["visible"]), json.dumps(state["visible"]))
        check("S-05", "it points at the start endpoint",
              state["href"] == "/api/auth/oidc/start", state["href"])

        # ---- 3. clicking it reaches the provider ---------------------
        page.click("#auth-sso-start")
        page.wait_for_timeout(2500)
        landed = page.url
        body = page.content()
        check("S-06", "clicking it redirects to the identity provider",
              f"127.0.0.1:{IDP_PORT}" in landed and "FAKE PROVIDER LOGIN" in body,
              landed[:120])
        check("S-07", "the authorization request carries PKCE and a nonce",
              "code_challenge_method=S256" in landed and "nonce=" in landed
              and "state=" in landed, landed.split("?")[-1][:160])
        check("S-08", "no client secret appears in the URL",
              "client_secret" not in landed, landed[:120])

        # ---- 4. a failed callback is explained -----------------------
        page.goto(f"{base}/?sso_error=no_account_and_provisioning_disabled",
                  wait_until="networkidle")
        page.wait_for_timeout(1500)
        note = page.evaluate("""() => {
            const el = document.querySelector('.auth-sso-error');
            return el ? el.innerText : '';
        }""")
        check("S-09", "a rejected sign-in is explained on the page",
              "no NetGravity account exists" in note, note[:140])

        check("S-10", "no page errors or CSP violations", not errors,
              json.dumps(errors[:3]))

        page.screenshot(path="sso_signin.png")
        browser.close()

    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed} passed, {len(results) - passed} failed of {len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
