from __future__ import annotations

from io import BytesIO

import pandas as pd


def references_to_excel(
    buildings: pd.DataFrame,
    meters: pd.DataFrame,
    gas_accounts: pd.DataFrame | None = None,
) -> bytes:
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        buildings.to_excel(writer, sheet_name="Buildings", index=False)
        meters.to_excel(writer, sheet_name="Meters", index=False)
        if gas_accounts is not None:
            gas_accounts.to_excel(writer, sheet_name="Gas Accounts", index=False)
    return out.getvalue()
