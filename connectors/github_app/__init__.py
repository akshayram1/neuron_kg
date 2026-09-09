"""Read-only GitHub App connector for repository knowledge ingestion."""

from .auth import GitHubAppSettings, GitHubConfigurationError

__all__ = ["GitHubAppSettings", "GitHubConfigurationError"]
