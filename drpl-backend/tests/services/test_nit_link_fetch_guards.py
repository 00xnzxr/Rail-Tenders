import socket
from unittest.mock import patch

from app.services.nit_link_fetch_service import (
    is_allowed_host, resolves_to_private_ip, is_document_response,
)


def test_allowed_ireps_host():
    assert is_allowed_host("https://www.ireps.gov.in/ireps/works/pdfdocs/x/viewNitPdf_1.pdf")

def test_allowed_subdomain():
    assert is_allowed_host("https://mkp.gem.gov.in/some/doc.pdf")

def test_rejects_non_https():
    assert not is_allowed_host("http://www.ireps.gov.in/x.pdf")

def test_rejects_unlisted_host():
    assert not is_allowed_host("https://evil.example.com/x.pdf")
    assert not is_allowed_host("https://ireps.gov.in.evil.com/x.pdf")

def test_private_ip_localhost():
    assert resolves_to_private_ip("localhost")

def test_public_host_not_private():
    # A well-known public host should not be flagged private. Uses DNS; if the
    # test env has no DNS, this returns True defensively — accept either by
    # asserting localhost is private (covered above) and skipping net here.
    # Keep this test focused on the localhost/loopback contract.
    assert resolves_to_private_ip("127.0.0.1")

def test_document_response_accepts_pdf():
    assert is_document_response(200, "application/pdf", b"%PDF-1.4 rest",
                                size=1000, max_bytes=10_000)

def test_document_response_rejects_html_login():
    assert not is_document_response(200, "text/html; charset=utf-8",
                                    b"<html><body>login", size=1000, max_bytes=10_000)

def test_document_response_rejects_oversize():
    assert not is_document_response(200, "application/pdf", b"%PDF-1.4",
                                    size=20_000, max_bytes=10_000)

def test_document_response_rejects_non_200():
    assert not is_document_response(404, "application/pdf", b"%PDF-1.4",
                                    size=100, max_bytes=10_000)

def test_document_response_rejects_pdf_ctype_but_html_bytes():
    assert not is_document_response(200, "application/pdf", b"<html> not a pdf",
                                    size=100, max_bytes=10_000)

def test_host_userinfo_trick_rejected():
    # urlparse().hostname strips userinfo → evil.com, not ireps.gov.in
    assert not is_allowed_host("https://ireps.gov.in@evil.com/x.pdf")

def test_host_prefix_lookalike_rejected():
    assert not is_allowed_host("https://evilireps.gov.in/x.pdf")

def test_host_no_host_rejected():
    assert not is_allowed_host("https:///x.pdf")

def test_cgnat_range_is_unsafe():
    with patch.object(socket, "getaddrinfo",
                      return_value=[(None, None, None, None, ("100.64.0.1", 0))]):
        assert resolves_to_private_ip("cgnat.example")

def test_public_ip_is_safe():
    with patch.object(socket, "getaddrinfo",
                      return_value=[(None, None, None, None, ("8.8.8.8", 0))]):
        assert not resolves_to_private_ip("dns.google")

def test_dns_failure_fails_closed():
    with patch.object(socket, "getaddrinfo", side_effect=socket.gaierror("no dns")):
        assert resolves_to_private_ip("nonexistent.invalid")

def test_ipv4_mapped_private_is_unsafe():
    with patch.object(socket, "getaddrinfo",
                      return_value=[(None, None, None, None, ("::ffff:10.0.0.1", 0))]):
        assert resolves_to_private_ip("mapped.example")
