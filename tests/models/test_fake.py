import pytest
from pydantic import BaseModel

from app.models.client import Message, Role
from app.models.fake import FakeModelClient
from tests.models.conftest import FIXTURES

USER = [Message(role="user", content="x")]


class Out(BaseModel):
    metric: str
    value: float


async def test_replays_fixtures_in_order_by_role_prompt_case() -> None:
    fake = FakeModelClient(FIXTURES)
    first = await fake.complete(Role.UTILITY, USER, prompt="utility_repair_json")
    assert first.content is not None and "top1_accuracy" in first.content
    second = await fake.structured(Role.UTILITY, USER, Out, prompt="utility_repair_json")
    assert second == Out(metric="f1", value=0.5)
    assert fake.calls == [
        (Role.UTILITY, "utility_repair_json", "default"),
        (Role.UTILITY, "utility_repair_json", "default"),
    ]


async def test_missing_fixture_is_a_clear_error() -> None:
    fake = FakeModelClient(FIXTURES, case="nope")
    with pytest.raises(FileNotFoundError, match=r"utility_repair_json/nope.json"):
        await fake.complete(Role.UTILITY, USER, prompt="utility_repair_json")


async def test_exhausted_script_is_an_error() -> None:
    fake = FakeModelClient(FIXTURES)
    for _ in range(2):
        await fake.complete(Role.UTILITY, USER, prompt="utility_repair_json")
    with pytest.raises(IndexError):
        await fake.complete(Role.UTILITY, USER, prompt="utility_repair_json")
