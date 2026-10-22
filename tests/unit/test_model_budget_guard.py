"""The settings refuse flagship OpenAI models unless explicitly allowed — a test key
funded with $5 was emptied once by a flagship model."""
import pytest

from config.settings import Settings


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna", "gpt-5.6-luna",
                                   "gpt-5.6-terra", "gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-4.1", "gpt-4o", "o3"])
def test_expensive_llm_models_are_refused(model):
    with pytest.raises(ValueError, match="expensive"):
        Settings(_env_file=None, openai_llm_model=model, database_url="unused")


@pytest.mark.parametrize("model", ["gpt-4.1-nano", "gpt-4.1-mini", "gpt-4o-mini", "gpt-5-nano", "gpt-5.4-mini"])
def test_cheap_models_are_allowed(model):
    assert Settings(_env_file=None, openai_llm_model=model, database_url="unused").openai_llm_model == model


def test_expensive_model_can_be_allowed_on_purpose():
    s = Settings(_env_file=None, openai_llm_model="gpt-6-luna", allow_expensive_openai_models=True, database_url="unused")
    assert s.openai_llm_model == "gpt-6-luna"


def test_defaults_are_the_cheap_measured_models():
    s = Settings(_env_file=None, database_url="unused")
    assert (s.openai_llm_model, s.openai_stt_model, s.openai_realtime_stt_model) == (
        "gpt-4.1-mini", "gpt-transcribe", "gpt-live-transcribe")
