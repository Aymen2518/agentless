"""Credentials for every GCP call: ADC, optionally impersonating a deployer service account."""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Any

SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
ENV_VAR = "AGENTLESS_IMPERSONATE_SERVICE_ACCOUNT"
FLAG = "--impersonate-service-account"
SA_EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*@[a-z0-9][a-z0-9.-]*\.gserviceaccount\.com$")


def check_service_account(email: str) -> str:
    """Return `email` stripped, or raise ValueError when it is not a service account email."""
    email = email.strip()
    if not SA_EMAIL_RE.match(email):
        raise ValueError(f"{email!r} is not a service account email (name@project.iam.gserviceaccount.com)")
    return email


@dataclass(frozen=True)
class Deployer:
    """Identity agentless acts as: plain ADC, or ADC impersonating `impersonate` through `delegates`."""

    impersonate: str | None = None
    delegates: tuple[str, ...] = ()
    source: str = field(default="ADC", compare=False)  # where the setting came from, for messages only

    @classmethod
    def from_chain(cls, value: str, source: str) -> Deployer:
        """Parse gcloud's `delegate1,delegate2,target` form; the last account is the one impersonated."""
        chain = [check_service_account(part) for part in value.split(",") if part.strip()]
        if not chain:
            raise ValueError(f"{source} is empty")
        return cls(chain[-1], tuple(chain[:-1]), source)

    def describe(self) -> str | None:
        """`target (via a, b)` for headers and locks, or None when not impersonating."""
        if not self.impersonate:
            return None
        via = f" via {', '.join(self.delegates)}" if self.delegates else ""
        return f"{self.impersonate}{via}"

    def credentials(self) -> Any:
        """Credentials for SDK clients; built once per identity, refreshed by google-auth as tokens expire."""
        return _credentials(self.impersonate, self.delegates)


@functools.cache
def _credentials(impersonate: str | None, delegates: tuple[str, ...]) -> Any:
    import google.auth
    from google.auth import impersonated_credentials

    source, _ = google.auth.default(scopes=list(SCOPES))
    if not impersonate:
        return source
    return impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=impersonate,
        target_scopes=list(SCOPES),
        delegates=list(delegates) or None,
    )
