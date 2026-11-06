from pydantic import BaseModel, Field, field_validator


def _norm_email(v: str) -> str:
    v = v.strip().lower()
    if "@" not in v or v.startswith("@") or v.endswith("@") or len(v) > 320:
        raise ValueError("invalid email")
    return v


class SignupIn(BaseModel):
    email: str
    password: str = Field(min_length=10, max_length=256)
    household_name: str = Field(min_length=1, max_length=200)

    _email = field_validator("email")(_norm_email)


class SignupOut(BaseModel):
    member_id: int
    household_id: int


class ConfirmIn(BaseModel):
    token: str


class LoginIn(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeOut(BaseModel):
    id: int
    email: str
    role: str
    household_id: int
