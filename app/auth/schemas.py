from pydantic import BaseModel, Field, field_validator, model_validator


def _norm_email(v: str) -> str:
    v = v.strip().lower()
    if "@" not in v or v.startswith("@") or v.endswith("@") or len(v) > 320:
        raise ValueError("invalid email")
    return v


class SignupIn(BaseModel):
    email: str
    password: str = Field(min_length=10, max_length=256)
    household_name: str | None = Field(default=None, min_length=1, max_length=200)
    invite_token: str | None = Field(default=None, max_length=200)

    _email = field_validator("email")(_norm_email)

    @model_validator(mode="after")
    def _exactly_one(self) -> "SignupIn":
        if (self.household_name is None) == (self.invite_token is None):
            raise ValueError("provide exactly one of household_name or invite_token")
        return self


class SignupOut(BaseModel):
    member_id: int
    household_id: int


class ConfirmIn(BaseModel):
    token: str


class LoginIn(BaseModel):
    email: str
    password: str = Field(max_length=256)  # caps argon2 work per request

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


class ConfirmResendIn(BaseModel):
    email: str = Field(max_length=320)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class ResetRequestIn(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class ResetConfirmIn(BaseModel):
    token: str
    new_password: str = Field(min_length=10, max_length=256)
