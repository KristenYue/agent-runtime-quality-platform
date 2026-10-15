"""Optional business adapters built on the provider-neutral runtime."""

from .opinion_agent import (
    OpinionAgentRuntimeAdapter,
    OpinionAnalysisRequest,
    OpinionComment,
)

__all__ = [
    "OpinionAgentRuntimeAdapter",
    "OpinionAnalysisRequest",
    "OpinionComment",
]
