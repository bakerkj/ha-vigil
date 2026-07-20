# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Layer 4 suppression: exclusion predicates + the startup/exclusion filter pass."""

from __future__ import annotations

from ..models import ExclusionConfig, IssueKind, VigilIssue, is_device_excluded


def suppress_issues(
    issues: list[VigilIssue],
    exclusions: ExclusionConfig,
    *,
    startup_grace_active: bool,
    staleness_exclusions: ExclusionConfig | None = None,
) -> list[VigilIssue]:
    """Apply Layer 4 suppression to a list of detected issues.

    During the startup grace every issue is suppressed. Otherwise drop issues
    whose device or integration is excluded; ``staleness_exclusions`` drops only
    SILENT_DEVICE (Engine 3) issues, leaving offline detection intact.
    """
    if startup_grace_active:
        return []
    kept: list[VigilIssue] = []
    for issue in issues:
        if is_device_excluded(issue.device_id, issue.domain, exclusions):
            continue
        if (
            staleness_exclusions is not None
            and issue.kind is IssueKind.SILENT_DEVICE
            and is_device_excluded(issue.device_id, issue.domain, staleness_exclusions)
        ):
            continue
        kept.append(issue)
    return kept
