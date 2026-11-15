"""Account contracts. Email format, password length and required profile fields are checked by the
service, so each rule answers with its own error code instead of the generic invalid_request."""

import unicodedata
from typing import Annotated, Literal

from pydantic import AfterValidator, ConfigDict, Field, model_validator

from api.schemas import StrictModel


def encodable(value: str) -> str:
    # JSON can carry lone surrogates; they cannot be hashed, stored or serialised again.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Texto con caracteres no válidos.") from None
    return value


def printable(value: str) -> str:
    # PostgreSQL text cannot hold NUL, and control characters have no place in a profile.
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise ValueError("No se admiten caracteres de control.")
    return value


def bounded(limit: int):
    return Annotated[str, Field(strict=True, max_length=limit), AfterValidator(printable)]


Raw = Annotated[str, Field(strict=True), AfterValidator(encodable)]


class AccountModel(StrictModel):
    """Passwords and tokens travel exactly as typed or issued: no whitespace stripping."""

    model_config = ConfigDict(str_strip_whitespace=False)


class LoginRequest(AccountModel):
    email: Raw
    password: Raw


class ProfileInput(StrictModel):
    """Trimmed text. full_name, agency and country are required, but the service reports
    their absence as incomplete_profile."""

    full_name: bounded(120) | None = None
    position: bounded(80) | None = None
    agency: bounded(120) | None = None
    acronym: bounded(20) | None = None
    country: bounded(120) | None = None


class RegistrationRequest(AccountModel):
    password: Raw
    profile: ProfileInput


class CreateUserRequest(AccountModel):
    email: Raw
    temporary_password: Raw | None = None


class UpdateUserRequest(AccountModel):
    email: Raw | None = None
    profile: ProfileInput | None = None

    @model_validator(mode="after")
    def something_to_change(self):
        if self.email is None and self.profile is None:
            raise ValueError("Indique email o profile.")
        return self


class TemporaryPasswordRequest(AccountModel):
    temporary_password: Raw | None = None


class Profile(AccountModel):
    full_name: str
    position: str
    agency: str
    acronym: str
    country: str


class UserResponse(AccountModel):
    id: str
    email: str
    role: Literal["admin", "agencia"]
    status: Literal["pendiente", "activo"]
    profile: Profile | None
    created_at: str
    registered_at: str | None
    temporary_expires_at: str | None


class LoginResponse(AccountModel):
    token: str
    expires_at: str
    user: UserResponse


class UserList(AccountModel):
    users: list[UserResponse]


class TemporaryPasswordResponse(AccountModel):
    user: UserResponse
    temporary_password: str
