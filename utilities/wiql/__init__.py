"""
WIQL (Work Item Query Language) Utilities for Azure DevOps.

This module provides:
- WIQLBuilder: Fluent API for building WIQL queries with ADO macro support
- DateMacro: Enum of available ADO date macros
- build_date_filter: Helper to build date filter clauses
- execute_wiql: Canonical WIQL execution against Azure DevOps REST API

Supports ADO-native date macros:
- @Today, @Today - N (days ago)
- @StartOfDay, @StartOfDay('-Nd') 
- @StartOfWeek, @StartOfMonth, @StartOfYear
- @Me (current user)
"""

from utilities.wiql.builder import (
    WIQLBuilder,
    DateMacro,
    build_date_filter,
)

__all__ = [
    "WIQLBuilder",
    "DateMacro",
    "build_date_filter",
]
