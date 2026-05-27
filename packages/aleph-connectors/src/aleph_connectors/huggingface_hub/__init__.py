"""HuggingFace Hub connector — returns model/dataset/paper cards as markdown sources."""

from aleph_connectors.huggingface_hub.register import (
    HuggingFaceHubConnector,
    HuggingFaceHubMetadata,
)

__all__ = ["HuggingFaceHubConnector", "HuggingFaceHubMetadata"]
