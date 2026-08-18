# ERAU Energy Dashboard

A Streamlit application for monthly FPL bill-history analytics at ERAU Daytona Beach.

Upload the latest enhanced FPL Excel workbook and the app will:

- validate and normalize the **Bill History** sheet;
- reconstruct FPL meter IDs from `billRaw.meterSymbol + meterNumber`;
- map meters to buildings, rate schedules, utilities and square footage;
- aggregate multiple meters to the correct building before EUI calculation;
- calculate cost, kWh, effective $/kWh, kWh/sf, $/sf and electric EUI;
- calculate previous-period cost, usage and EUI variances;
- calculate rolling 12-month building metrics;
- flag unmapped meters, unresolved shared meters, missing floor area and other data-quality issues;
- propose mappings for newly observed meters when the same FPL account has history tied to a single known building;
- let an administrator add/edit buildings and meter mappings in the app;
- export an analyzed Excel workbook and CSV outputs.

## Electric EUI

The application currently calculates **electricity-only site EUI**:

```text
Electric EUI (kBtu/sf) = electricity kWh × 3.412 / gross floor area (sf)
```

`rolling12ElectricEUI` uses rolling 12-month electricity use and is therefore expressed as kBtu/sf/year once 12 months of data are available.

Central plants, pump houses, exterior loads and other special facilities are retained, but they are separated from normal-building peer rankings. Buildings with no floor area are not assigned EUI.

## Repository layout

```text
erau-energy-dashboard/
├── app.py
├── requirements.txt
├── README.md
├── data/
│   ├── buildings_seed.csv
│   └── meters_seed.csv
├── src/
│   ├── analytics.py
│   ├── exporter.py
│   ├── loader.py
│   ├── reference_io.py
│   └── storage.py
├── tests/
│   ├── test_analytics.py
│   └── test_loader.py
└── .streamlit/
    ├── config.toml
    └── secrets.toml.example
```

## Run locally

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate       # macOS/Linux
# .venv\Scripts\activate        # Windows

pip install -r requirements.txt
streamlit run app.py
```

The first run creates `data/dashboard.db` and seeds it from the two reference CSV files.

## Monthly workflow

1. Run the FPL collector and create the enhanced `.xlsx` workbook.
2. Open the dashboard.
3. Upload the workbook in the sidebar.
4. Review **Data Quality** for new/unmapped meters.
5. Confirm new mappings in **Reference Data**.
6. Review Overview, Cost & Usage, Building EUI, and Variance & Exceptions.
7. Download the analyzed workbook if desired.

The monthly workbook is not stored by the app. Analytics are generated from the uploaded workbook in memory.

## Add a new building

Open **Reference Data → Buildings** and enter:

- Building ID
- Property ID
- Facility name
- Use type
- Campus
- Gross floor area (sf)
- EUI category
- Notes

After the building exists, add its meters under **Reference Data → Meters**.

## Add a new meter

Open **Reference Data → Meters** and enter:

- Meter ID
- Building ID
- Utility
- Rate schedule
- Campus
- Status (`Current`, `Historical`, `Shared`, etc.)
- Allocation fraction
- Notes

If an upload contains a new meter, **Data Quality** will show it. When an FPL account has known meter history for only one building, the app creates a high-confidence building suggestion. Suggestions never affect EUI until an administrator saves the mapping.

### Shared meters

If one physical meter serves multiple buildings, create one row for each building and enter allocation fractions that total exactly `1.0`.

Example:

```text
ACD4331 → Building 1600   allocation 0.40
ACD4331 → Building 1624   allocation 0.60
```

Until all allocations are present and total 100%, that meter is excluded from building EUI to prevent double counting.

## Historical/replacement meters

Do not delete an old meter merely because FPL replaced it. Change its status to `Historical` and add the replacement meter. Historical meters remain valid for prior bill history and rolling EUI.

## Persistent reference edits in production

### Local use

No setup is required. The app uses SQLite at:

```text
data/dashboard.db
```

### Hosted Streamlit deployment

The filesystem on a hosted application should not be treated as durable reference storage. For production, create a small hosted Postgres database (Supabase, Neon, Railway, Render, etc.) and set a Streamlit secret:

```toml
DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require"
```

The application will create and seed the `buildings` and `meters` tables automatically on first connection.

Optionally protect reference edits:

```toml
ADMIN_PASSWORD = "your-admin-password"
```

If `ADMIN_PASSWORD` is not configured, reference editing is available without a password.

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` for local secret testing. Never commit the real secrets file.

## Deploy to Streamlit Community Cloud

1. Push this repository to GitHub.
2. Create a Streamlit Community Cloud app from the repository.
3. Set the entry point to `app.py`.
4. Add `DATABASE_URL` and optionally `ADMIN_PASSWORD` in the app's Secrets settings.
5. Deploy.

## Expected workbook schema

The uploaded workbook must contain a sheet named **Bill History** with at least:

- `accountNumber`
- `dateBilled`
- `meterNumber`
- `kWh`
- `totalBillAmount`

For meter reconstruction, the enhanced workbook should also include `billRaw.meterSymbol`. If it is absent, the application can only match meter IDs that are represented directly by `meterNumber`.

The ERAU enhanced collector workbook already contains the required fields plus billing-period, payment and raw bill-history fields.

## Data-quality safeguards

The application intentionally does not silently guess when reference data are ambiguous.

- **Unmapped meter:** excluded from building analytics and shown for review.
- **Shared meter without a complete 100% allocation:** excluded from building analytics.
- **Missing square footage:** cost/use remain visible; EUI is blank.
- **Historical meter:** remains mapped and usable.
- **Unmetered lighting:** retained in reference data but does not match FPL Bill History unless a bill record has a corresponding meter ID.
- **New meter suggestion:** advisory only until an administrator saves it.

## Tests

```bash
pytest -q
```

The tests cover meter-ID reconstruction, previous-bill variance, EUI math, and unmapped-meter handling.

## Responsive layout

The app uses a responsive dashboard layout for laptop, tablet, and narrow browser widths:

- KPI cards are limited to three per row instead of six-wide rows.
- Charts and dataframes use Streamlit stretch width.
- Main content padding scales with the viewport.
- The sidebar is capped on laptop screens so it does not consume excessive horizontal space.
- KPI typography scales down at narrower breakpoints.

If you customize the dashboard, prefer short `st.columns(...)` groups and avoid forcing five or six metrics into one row.


## App password

The entire dashboard is protected by a shared password. `APP_PASSWORD` is required.

For local use, copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and set:

```toml
APP_PASSWORD = "your-strong-password"
```

Do **not** commit `.streamlit/secrets.toml`; it is already ignored by `.gitignore`.

For Streamlit Community Cloud, open the deployed app settings and place `APP_PASSWORD` in the **Secrets** section. The app renders no workbook data, reference data, or dashboard navigation until the correct password is entered.

`ADMIN_PASSWORD` remains optional and can be used as a second password specifically for adding/editing buildings and meters.
