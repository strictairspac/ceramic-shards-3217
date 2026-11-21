from typing import Any

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class ReceiptForm(BaseModel):
    step: str = Field(min_length=1, max_length=32)
    outcome: str = Field(min_length=1, max_length=16)
    content: dict[str, Any]
    note: str = Field(default='', max_length=500)
