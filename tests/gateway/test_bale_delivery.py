"""Exercise standalone Bale delivery through actual HTTP request serialization."""

from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest

from gateway.config import PlatformConfig
from plugins.platforms.bale import adapter


@pytest.fixture
def delivery_transport(monkeypatch):
    """Capture serialized requests without contacting a messaging service."""
    requests = []
    responses = []

    def handle(request):
        """Read the multipart stream while the source files remain open."""
        request.read()
        requests.append(request)
        return responses.pop(0) if responses else httpx.Response(
            200, json={"ok": True, "result": {"message_id": len(requests)}},
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(adapter.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    return requests, responses


@pytest.mark.asyncio
@pytest.mark.parametrize("force_document", [False, True])
async def test_registered_sender_delivers_files_without_text(
    tmp_path, delivery_transport, force_document,
):
    """Cron's registered sender uploads every file with its bytes and filename."""
    class Context:
        def register_platform(self, **kwargs):
            """Capture the callback used by standalone callers."""
            self.sender = kwargs["standalone_sender_fn"]

    context = Context()
    adapter.register(context)
    paths = [tmp_path / "report.txt", tmp_path / "image.png"]
    for path in paths:
        path.write_bytes(b"\x00\xffHermes attachment\n")
    result = await context.sender(
        PlatformConfig(token="test-token"), "42", "",
        media_files=[str(path) for path in paths], force_document=force_document,
        thread_id="unsupported-topic",
    )
    requests, _ = delivery_transport
    assert result == {"success": True, "message_id": "2"}
    assert len(requests) == 2
    for request, path in zip(requests, paths):
        assert str(request.url) == "https://tapi.bale.ai/bottest-token/sendDocument"
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
            + request.content,
        )
        parts = {part.get_param("name", header="content-disposition"): part
                 for part in message.iter_parts()}
        assert set(parts) == {"chat_id", "document"}
        assert parts["chat_id"].get_payload(decode=True) == b"42"
        assert parts["document"].get_filename() == path.name
        assert parts["document"].get_payload(decode=True) == path.read_bytes()


@pytest.mark.asyncio
async def test_missing_later_file_prevents_partial_delivery(tmp_path, delivery_transport):
    """Reject a missing attachment before sending accompanying text or other files."""
    path = tmp_path / "exists.txt"
    path.write_text("test")
    result = await adapter._standalone_send(
        PlatformConfig(token="test-token"), "42", "Report",
        media_files=[str(path), str(tmp_path / "missing.txt")],
    )
    assert "does not exist" in result["error"]
    assert delivery_transport[0] == []


@pytest.mark.asyncio
async def test_unreadable_file_returns_error(tmp_path, monkeypatch, delivery_transport):
    """Filesystem errors are returned through the delivery contract."""
    path = tmp_path / "report.txt"
    path.write_text("test")

    def fail_open(*args, **kwargs):
        """Model a file becoming unreadable after path validation."""
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "open", fail_open)
    result = await adapter._standalone_send(
        PlatformConfig(token="test-token"), "42", "", media_files=[str(path)],
    )
    assert result == {"error": "permission denied"}
    assert delivery_transport[0] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400])
async def test_api_failure_stops_delivery_and_redacts_token(tmp_path, delivery_transport, status):
    """Transport and API errors must stop subsequent files without leaking tokens."""
    requests, responses = delivery_transport
    responses.append(httpx.Response(status, json={
        "ok": False, "description": "failed with test-token",
    }))
    path = tmp_path / "report.txt"
    path.write_text("test")
    result = await adapter._standalone_send(
        PlatformConfig(token="test-token"), "42", "", media_files=[str(path)] * 2,
    )
    assert "error" in result
    assert "test-token" not in result["error"]
    assert len(requests) == 1
