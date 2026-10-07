"""OpenAI → local fallback (llm/fallback.py, speech/*/fallback.py, llm/openai_client.py).
Fakes only — no network, no models."""
import httpx
import openai
import pytest

from config.settings import get_settings
from llm.base import LLMProposal, LLMProvider
from llm.fallback import FallbackLLMProvider
from llm.openai_client import is_openai_account_error, reset_openai_health, use_openai
from speech.tts.base import TTSProvider
from speech.tts.fallback import FallbackTTSProvider

pytestmark = pytest.mark.asyncio


def _auth_error() -> openai.AuthenticationError:
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/x"))
    return openai.AuthenticationError("Incorrect API key provided", response=response, body=None)


def _quota_error() -> openai.RateLimitError:
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/x"))
    return openai.RateLimitError("You exceeded your current quota (insufficient_quota)", response=response, body=None)


@pytest.fixture(autouse=True)
def auto_mode(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "ai_backend", "auto")
    monkeypatch.setattr(s, "openai_api_key", "sk-test-not-real")
    # Simulated OpenAI failures must not start loading a real local LLM.
    monkeypatch.setattr("llm.openai_client._start_local_llm_warmup", lambda: None)
    reset_openai_health()
    yield
    reset_openai_health()


class FakeLLM(LLMProvider):
    def __init__(self, deltas=("{}",), error: Exception | None = None, error_after: int = 0):
        self.deltas, self.error, self.error_after, self.calls = list(deltas), error, error_after, 0

    async def propose(self, messages, json_schema, schema_name="agent_response"):
        self.calls += 1
        if self.error:
            raise self.error
        return LLMProposal(raw_text="".join(self.deltas), model="fake")

    async def propose_stream(self, messages, json_schema, schema_name="agent_response"):
        self.calls += 1
        for i, d in enumerate(self.deltas):
            if self.error and i == self.error_after:
                raise self.error
            yield d
        if self.error and self.error_after >= len(self.deltas):
            raise self.error


async def _collect(gen):
    return [x async for x in gen]


def test_account_errors_are_recognised_and_ordinary_ones_are_not():
    assert is_openai_account_error(_auth_error())
    assert is_openai_account_error(_quota_error())
    assert not is_openai_account_error(RuntimeError("boom"))


def test_mode_switch(monkeypatch):
    s = get_settings()
    assert use_openai()
    monkeypatch.setattr(s, "ai_backend", "local")
    assert not use_openai()
    monkeypatch.setattr(s, "ai_backend", "auto")
    monkeypatch.setattr(s, "openai_api_key", "")
    assert not use_openai()  # no key configured → local


async def test_llm_fails_over_on_account_error_and_stays_local():
    primary = FakeLLM(error=_auth_error())
    secondary = FakeLLM(deltas=["{\"a\":", "1}"])
    llm = FallbackLLMProvider(primary, secondary)
    assert await _collect(llm.propose_stream([], {})) == ["{\"a\":", "1}"]
    assert llm.backend == "local"
    assert not use_openai()  # marked down
    await _collect(llm.propose_stream([], {}))
    assert primary.calls == 1  # not retried while down
    assert secondary.calls == 2


async def test_llm_does_not_fail_over_on_an_ordinary_error():
    llm = FallbackLLMProvider(FakeLLM(error=RuntimeError("500 from upstream")), FakeLLM())
    with pytest.raises(RuntimeError):
        await _collect(llm.propose_stream([], {}))
    assert use_openai()


async def test_llm_never_splices_backends_mid_stream():
    # OpenAI already produced output (the agent may be speaking it) — switching model
    # half-way would be incoherent, so the error surfaces instead.
    llm = FallbackLLMProvider(FakeLLM(deltas=["{\"state\":", "x"], error=_auth_error(), error_after=1), FakeLLM())
    with pytest.raises(openai.AuthenticationError):
        await _collect(llm.propose_stream([], {}))


async def test_local_mode_never_calls_openai(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_backend", "local")
    primary, secondary = FakeLLM(), FakeLLM(deltas=["ok"])
    await FallbackLLMProvider(primary, secondary).propose([], {})
    assert primary.calls == 0 and secondary.calls == 1


async def test_openai_only_mode_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_backend", "openai")
    with pytest.raises(openai.AuthenticationError):
        await FallbackLLMProvider(FakeLLM(error=_auth_error()), FakeLLM()).propose([], {})


class FakeTTS(TTSProvider):
    def __init__(self, chunks=(b"\x01\x00",), error=None):
        self.chunks, self.error, self.calls = list(chunks), error, 0

    async def synthesize_stream(self, text):
        self.calls += 1
        if self.error:
            raise self.error
        for c in self.chunks:
            yield c

    async def cancel(self):
        pass

    async def close(self):
        pass


async def test_tts_fails_over_before_the_first_chunk():
    tts = FallbackTTSProvider(FakeTTS(error=_quota_error()), FakeTTS(chunks=[b"\x02\x00", b"\x03\x00"]))
    assert await _collect(tts.synthesize_stream("hi")) == [b"\x02\x00", b"\x03\x00"]
    assert tts.backend == "local"


async def test_stt_fails_over_with_the_whole_utterance(monkeypatch):
    import speech.stt.fallback as fb

    async def openai_fails(pcm, model, language="", client=None):
        raise _auth_error()

    received = {}

    def local_ok(pcm):
        received["bytes"] = len(pcm)
        return "yes this is me"

    monkeypatch.setattr(fb, "transcribe_pcm16_openai", openai_fails)
    monkeypatch.setattr(fb, "transcribe_pcm16", local_ok)
    provider = fb.FallbackSTTProvider()
    stream = await provider.start_stream()
    for _ in range(10):
        await stream.send_audio(b"\x00\x01" * 1600)  # 10 x 100 ms
    result = await stream.receive_final()
    assert result.text == "yes this is me"
    assert received["bytes"] == 10 * 3200
    assert provider.backend == "local"
