"""Single-provider OpenID Connect authorization code flow with pinned RS256 signing."""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
from urllib import request
from urllib.parse import quote_plus, urlencode, urlsplit

from connections import NoRedirect


def issuer_url(value):
    p = urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment or p.query:
        raise ValueError("Use the provider's exact HTTPS OIDC issuer URL.")
    if any(c.isspace() or c == "\\" for c in value):
        raise ValueError("Use a valid HTTPS OIDC URL.")
    return value


def portable_hash(value):
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def json_request(url, payload=None, headers=None):
    req = request.Request(url, data=urlencode(payload).encode("ascii") if payload is not None else None,
                          headers={"Accept": "application/json", **(headers or {})})
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=10) as response:
            raw = response.read(262145)
        if len(raw) <= 262144:
            result = json.loads(raw)
            if isinstance(result, dict):
                return result
    except (OSError, ValueError) as exc:
        raise PermissionError("The identity provider did not confirm sign-in.") from exc
    raise PermissionError("The identity provider returned an invalid sign-in response.")


class OIDC:
    def __init__(self, origin, issuer, client_id, secret_env=None):
        try:
            import jwt
        except ImportError as exc:
            raise ValueError("Install the OIDC dependencies from requirements-managed.txt.") from exc
        self.jwt = jwt
        self.issuer = issuer_url(issuer)
        self.client_id = client_id
        self.secret = os.environ.get(secret_env) if secret_env else None
        if secret_env and not self.secret:
            raise ValueError(f"Set {secret_env} before starting Penstock.")
        self.redirect = origin + "/api/auth/oidc/callback"
        self.discovery = None
        self.keys = None
        self.lock = threading.Lock()

    def metadata(self):
        with self.lock:
            if self.discovery and self.discovery[0] > time.monotonic():
                return self.discovery[1]
            meta = json_request(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
            auth_methods = meta.get("token_endpoint_auth_methods_supported", [])
            method = "client_secret_basic" if self.secret else "none"
            if (meta.get("issuer") != self.issuer or "RS256" not in meta.get("id_token_signing_alg_values_supported", [])
                    or "code" not in meta.get("response_types_supported", [])
                    or "code_challenge_methods_supported" in meta and "S256" not in meta["code_challenge_methods_supported"]
                    or auth_methods and method not in auth_methods):
                raise PermissionError("The provider does not advertise the configured OIDC sign-in flow.")
            try:
                for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                    issuer_url(meta[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise PermissionError("The provider returned invalid OIDC metadata.") from exc
            self.discovery = (time.monotonic() + 300, meta)
            return meta

    def authorization(self, state, nonce, verifier):
        endpoint = self.metadata()["authorization_endpoint"]
        query = urlencode({"client_id": self.client_id, "redirect_uri": self.redirect, "response_type": "code",
                           "scope": "openid email", "state": state, "nonce": nonce,
                           "code_challenge": portable_hash(verifier), "code_challenge_method": "S256"})
        return endpoint + "?" + query

    def signing_key(self, kid):
        uri = self.metadata()["jwks_uri"]
        with self.lock:
            now = time.monotonic()
            if not self.keys or self.keys[0] != uri or self.keys[1] < now - 300:
                self.keys = (uri, now, json_request(uri).get("keys", []))
            for _ in range(2):
                candidates = [j for j in self.keys[2] if isinstance(j, dict) and j.get("kid") == kid and
                              j.get("kty") == "RSA" and j.get("alg", "RS256") == "RS256" and
                              j.get("use", "sig") == "sig" and "verify" in j.get("key_ops", ["verify"])]
                if len(candidates) == 1:
                    key = self.jwt.PyJWK.from_dict(candidates[0], algorithm="RS256").key
                    if key.key_size >= 2048:
                        return key
                    break
                if now - self.keys[1] < 30:
                    break
                self.keys = (uri, now, json_request(uri).get("keys", []))
        raise PermissionError("The identity provider did not present a trusted signing key.")

    def validate(self, signed, nonce, access_token=None):
        jwt = self.jwt
        if not isinstance(signed, str) or len(signed) > 16384:
            raise PermissionError("The identity provider did not return a signed ID token.")
        try:
            header = jwt.get_unverified_header(signed)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not 1 <= len(kid) <= 256:
                raise PermissionError("The identity provider did not present a trusted signing algorithm.")
            claims = jwt.decode(signed, self.signing_key(kid), algorithms=["RS256"], audience=self.client_id,
                                issuer=self.issuer, leeway=30,
                                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]})
            given_nonce, subject = claims["nonce"], claims["sub"]
            if not isinstance(given_nonce, str) or not given_nonce.isascii() or not hmac.compare_digest(given_nonce, nonce):
                raise PermissionError("The signed sign-in response is for a different session.")
            if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
                raise PermissionError("The identity provider did not return a stable subject.")
            audience, party = claims["aud"], claims.get("azp")
            if (isinstance(audience, list) and len(audience) > 1 or party is not None) and party != self.client_id:
                raise PermissionError("The signed response is for a different OIDC client.")
            if "at_hash" in claims:
                if not isinstance(access_token, str) or not access_token.isascii():
                    raise PermissionError("The provider returned an invalid token binding.")
                expected = base64.urlsafe_b64encode(hashlib.sha256(access_token.encode("ascii")).digest()[:16]).decode("ascii").rstrip("=")
                if not isinstance(claims["at_hash"], str) or not claims["at_hash"].isascii() or not hmac.compare_digest(claims["at_hash"], expected):
                    raise PermissionError("The provider returned an invalid token binding.")
            return claims
        except (jwt.PyJWTError, TypeError, ValueError, KeyError) as exc:
            raise PermissionError("The identity provider's signed response could not be verified.") from exc

    def exchange(self, code, nonce, verifier):
        payload = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect,
                   "code_verifier": verifier}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.secret:
            auth = quote_plus(self.client_id) + ":" + quote_plus(self.secret)
            headers["Authorization"] = "Basic " + base64.b64encode(auth.encode("ascii")).decode("ascii")
        else:
            payload["client_id"] = self.client_id
        response = json_request(self.metadata()["token_endpoint"], payload, headers)
        return self.validate(response.get("id_token"), nonce, response.get("access_token"))
