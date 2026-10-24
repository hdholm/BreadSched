"""Serving a book through a TLS reverse proxy (#358).

**Experimental** (branch ``claude/experimental-book-server``). The server still
listens on loopback over plain HTTP; what changes is what a proxy in front of it
needs. A configured trusted origin lets the proxy's pages write without the proxy
rewriting ``Origin``; a token file keeps the token out of the terminal and stable
across restarts; and the HTTP client speaks HTTPS, verifying the proxy's
certificate and presenting a client certificate when asked.

The HTTPS tests stand in a TLS-wrapped listener for the proxy and need the
``openssl`` command to make their certificates; they are skipped without it.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import stat
import subprocess
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from breadsched.api import ApiError
from breadsched.api.http_client import HttpBookClient
from breadsched.cli.main import main
from breadsched.web.transport import serve, token_from_file, trusted_origin

PUBLIC = "https://books.example.lan"


def _start(db, **options):
    httpd = serve(db, host="127.0.0.1", port=0, **options)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _stop(httpd) -> None:
    httpd.shutdown()
    httpd.server_close()


def _undo(httpd, origin: str | None) -> int:
    headers = {"X-BreadSched-Token": httpd.token, "Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(
        f"http://127.0.0.1:{httpd.server_port}/api/v1/undo",
        data=b"{}",
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_a_trusted_origin_is_an_https_origin_and_nothing_more():
    assert trusted_origin("https://Books.Example.lan/") == "https://books.example.lan"
    assert trusted_origin("https://books.example.lan:443") == "https://books.example.lan"
    assert trusted_origin("https://books.example.lan:8443") == "https://books.example.lan:8443"
    for refused in (
        "http://books.example.lan",
        "https://books.example.lan/breadsched",
        "https://user@books.example.lan",
        "https://books.example.lan/?x=1",
        "books.example.lan",
        "https://",
    ):
        with pytest.raises(ValueError):
            trusted_origin(refused)


def test_only_the_configured_origin_may_write_besides_loopback(db, book):
    plain = _start(db)
    try:
        assert _undo(plain, PUBLIC) == 403
        assert _undo(plain, "http://127.0.0.1") == 200
    finally:
        _stop(plain)
    proxied = _start(db, trusted_origins=(PUBLIC,))
    try:
        assert _undo(proxied, PUBLIC) == 200
        assert _undo(proxied, None) == 200
        assert _undo(proxied, "https://evil.example") == 403
        assert _undo(proxied, PUBLIC + ":8443") == 403
    finally:
        _stop(proxied)
    with pytest.raises(ValueError):
        serve(db, host="127.0.0.1", port=0, trusted_origins=("http://books.example.lan",))


def test_a_token_file_is_private_and_kept_across_restarts(db, book, tmp_path):
    path = tmp_path / "server.token"
    first = token_from_file(path)
    assert token_from_file(path) == first and len(first) >= 32
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    httpd = _start(db, token=first)
    try:
        assert httpd.token == first
        assert HttpBookClient(f"http://127.0.0.1:{httpd.server_port}", first).revision >= 0
    finally:
        _stop(httpd)

    short = tmp_path / "short.token"
    short.write_text("too-short\n", encoding="utf-8")
    if os.name == "posix":
        short.chmod(0o600)
    with pytest.raises(ValueError, match="at least"):
        token_from_file(short)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_a_token_file_others_can_read_is_refused(tmp_path):
    path = tmp_path / "shared.token"
    path.write_text("x" * 40 + "\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="other users"):
        token_from_file(path)


def test_serve_takes_its_token_from_the_file_and_does_not_print_it(
    book_path_factory, tmp_path, monkeypatch, capsys
):
    import breadsched.web.transport as transport

    started = []
    real_serve = transport.serve

    def capture(*args, **kwargs):
        httpd = real_serve(*args, **kwargs)
        started.append(httpd)
        threading.Timer(0.5, httpd.shutdown).start()
        return httpd

    monkeypatch.setattr(transport, "serve", capture)
    monkeypatch.setattr("breadsched.api.discovery.runtime_directory", lambda: tmp_path / "run")
    book = book_path_factory()
    token_path = tmp_path / "serve.token"
    command = ["serve", book, "--port", "0", "--token-file", str(token_path)]
    assert main([*command, "--trusted-origin", PUBLIC]) == 0
    printed = capsys.readouterr().out
    token = token_path.read_text(encoding="utf-8").strip()
    assert started[0].token == token
    assert token not in printed
    assert f"the token is in {token_path}" in printed
    assert "Pages from the trusted origins given may also use it." in printed

    assert main([*command, "--trusted-origin", "http://books.example.lan"]) == 2
    assert "https://host" in capsys.readouterr().err


@pytest.fixture
def book_path_factory(tmp_path):
    from datetime import date

    from breadsched.gen.sample_book import create_sample_book

    def make() -> str:
        path = tmp_path / "served.breadsched"
        if not path.exists():
            create_sample_book(path, as_of=date(2026, 3, 15))
        return str(path)

    return make


# ---------------------------------------------------------------- HTTPS client


def _openssl(*args: str, cwd) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    """A test CA, a server certificate for localhost, and a client certificate."""
    if shutil.which("openssl") is None:
        pytest.skip("needs the openssl command to make test certificates")
    where = tmp_path_factory.mktemp("tls")
    curve = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
    _openssl(
        "req", "-x509", *curve, "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
        "-subj", "/CN=BreadSched test CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "subjectKeyIdentifier=hash",
        cwd=where,
    )  # fmt: skip
    for name, usage, san in (
        ("server", "serverAuth", "subjectAltName=DNS:localhost,IP:127.0.0.1\n"),
        ("client", "clientAuth", ""),
    ):
        (where / f"{name}.ext").write_text(
            san
            + f"extendedKeyUsage={usage}\n"
            + "basicConstraints=critical,CA:FALSE\n"
            + "keyUsage=critical,digitalSignature\n"
            + "subjectKeyIdentifier=hash\n"
            + "authorityKeyIdentifier=keyid,issuer\n",
            encoding="utf-8",
        )
        _openssl(
            "req", *curve, "-keyout", f"{name}.key", "-out", f"{name}.csr",
            "-subj", f"/CN={name}",
            cwd=where,
        )  # fmt: skip
        _openssl(
            "x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-out", f"{name}.pem", "-days", "2",
            "-extfile", f"{name}.ext",
            cwd=where,
        )  # fmt: skip
    return SimpleNamespace(
        ca=str(where / "ca.pem"),
        server=(str(where / "server.pem"), str(where / "server.key")),
        client=(str(where / "client.pem"), str(where / "client.key")),
    )


@pytest.fixture(params=["server-only", "client-certificates"])
def tls_server(request, db, book, certificates):
    """The book server behind a TLS listener, as a proxy would put it."""
    httpd = serve(db, host="127.0.0.1", port=0)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(*certificates.server)
    if request.param == "client-certificates":
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(certificates.ca)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield SimpleNamespace(
        httpd=httpd,
        url=f"https://localhost:{httpd.server_port}",
        client_certificates=request.param == "client-certificates",
    )
    _stop(httpd)


def test_the_client_reaches_a_book_over_verified_https(tls_server, certificates):
    from breadsched.gen.services import BalanceQuery

    httpd, url = tls_server.httpd, tls_server.url
    cert, key = certificates.client
    client = HttpBookClient(
        url, httpd.token, ca_file=certificates.ca, client_cert=cert, client_key=key
    )
    assert client.revision >= 0
    checking = client.find_account("Assets:Checking")
    assert client.account_balance(BalanceQuery(checking.handle)).account.handle == checking.handle

    # An unverified certificate is refused, as is a missing client certificate
    # when the listener asks for one.
    with pytest.raises(ApiError) as unverified:
        _ = HttpBookClient(url, httpd.token).revision
    assert (unverified.value.status, unverified.value.code) == (503, "server.unreachable")
    without_certificate = HttpBookClient(url, httpd.token, ca_file=certificates.ca)
    if tls_server.client_certificates:
        with pytest.raises(ApiError):
            _ = without_certificate.revision
    else:
        assert without_certificate.revision >= 0


def test_certificates_belong_to_https_servers_only(certificates):
    with pytest.raises(ValueError, match="https"):
        HttpBookClient("http://127.0.0.1:8765", "token", ca_file=certificates.ca)
    with pytest.raises(ValueError):
        HttpBookClient("ftp://127.0.0.1", "token")


def test_commands_use_an_https_server(tls_server, certificates, tmp_path, capsys):
    token_file = tmp_path / "token"
    token_file.write_text(tls_server.httpd.token, encoding="utf-8")
    cert, key = certificates.client
    command = ["balance", "unused.breadsched", "Assets:Checking", "--json"]
    command += ["--server", tls_server.url, "--token-file", str(token_file)]
    command += ["--ca-file", certificates.ca, "--client-cert", cert, "--client-key", key]
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["account"] == "Assets:Checking"

    missing = [*command[:-6], "--ca-file", str(tmp_path / "missing.pem")]
    assert main(missing) == 2
    assert "could not use --server" in capsys.readouterr().err
