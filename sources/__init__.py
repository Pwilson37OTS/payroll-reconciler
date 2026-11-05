"""Automated data sources that feed the reconciler engine.

Each connector produces the SAME normalized frames the manual uploads did, so
the engine and UI are unchanged:

* :mod:`sources.snowflake_bullhorn` — Bullhorn payable charges from Snowflake.
* :mod:`sources.sharepoint_payroll` — payroll register from a SharePoint library.
"""

from . import qbo_c2c, sharepoint_payroll, snowflake_bullhorn

__all__ = ["snowflake_bullhorn", "sharepoint_payroll", "qbo_c2c"]
