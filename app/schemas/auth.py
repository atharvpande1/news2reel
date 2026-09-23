from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize_email(value: str) -> str:
    # lower(), not casefold(): it has to agree with the users_email_lowercase
    # check constraint, which is Postgres lower().
    return value.strip().lower()


class LoginRequest(BaseModel):
    # A plain string, not EmailStr: login is an exact match against a row
    # inserted by hand, so RFC validation would reject nothing useful.
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_email(value)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
