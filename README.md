
## Monthly FPL upload workflow

The Streamlit app now owns the monthly append process. Upload the master FPL workbook once to initialize history. After that, upload only the newest monthly `.xls` or `.xlsx` file in the Streamlit sidebar. The app normalizes it, upserts electric bills by `accountNumber + dateBilled`, extracts/upserts demand history, and immediately reruns the dashboard against all stored history. No Marimo append step is required.

# ERAU Energy Dashboard

A password-protected Streamlit dashboard for ERAU Daytona Beach utility analytics.

The application keeps the existing FPL electricity workflow intact and adds a **separate, additive natural-gas module** for TECO and Gas South. Electricity and gas are combined only at the building analytics layer for Total Site EUI.

## What the app does

### Existing FPL electricity module — unchanged

Upload the enhanced FPL workbook and the app continues to:

- validate and normalize **Bill History**;
- reconstruct FPL meter IDs from `billRaw.meterSymbol + meterNumber`;
- map electric meters to buildings, square footage, rate schedules and allocations;
- calculate cost, kWh, effective $/kWh, kWh/sf, $/sf and electric EUI;
- calculate cost, usage and EUI variance;
- parse demand from **Selected Enriched** / **PDF Details**;
- calculate billed peak kW, on-peak/maximum demand, W/sf and demand charges;
- retain demand history in the database for month-over-month demand variance;
- flag unmapped/replacement/shared meters and missing floor area;
- let administrators add/edit buildings and meter mappings.

### New natural-gas module

Gas is stored separately from FPL data. Upload a normalized gas Excel/CSV file and the app will:

- store TECO and Gas South invoice history by month;
- map provider accounts to buildings and physical gas usage groups;
- calculate therms, gas cost, cost/therm and month-over-month variance;
- calculate rolling-12-month therms and gas cost;
- calculate **Gas EUI** in kBtu/sf/year;
- calculate gas $/sf;
- report zero-usage bills with fixed charges;
- compare provider/billing-component costs;
- flag unmapped and unregistered gas accounts;
- let administrators add/edit gas provider accounts;
- retain gas history in the same database across monthly uploads.

### Total Site EUI

The combined page calculates:

```text
Electric EUI = kWh × 3.412 / sf
Gas EUI      = therms × 100 / sf
Total Site EUI = Electric EUI + Gas EUI
```

Gas accounts marked `include_in_site_eui = False` are excluded from Gas EUI. The seeded **Building 610 Student Union generator** account is excluded by default.

## Important: TECO + Gas South therm de-duplication

Some buildings have both a TECO invoice and a Gas South invoice for the **same physical gas consumption**. Those therms must not be added together.

The dashboard uses a `usage_group` to connect the two provider accounts for the same service. Within each building/month/usage-group:

- all provider **costs are summed**;
- therms come from the row marked `usage_authoritative = True`;
- if no row is marked authoritative, the maximum reported therm value is used;
- the resulting physical therm usage is counted once for Gas EUI.

The seeded TECO/Gas South account mappings supplied for the campus already use this structure.

## Repository layout

```text
erau-energy-dashboard/
├── app.py
├── requirements.txt
├── README.md
├── pytest.ini
├── .python-version
├── .streamlit/
│   ├── config.toml
│   └── secrets.toml.example
├── data/
│   ├── buildings_seed.csv
│   ├── meters_seed.csv
│   ├── gas_accounts_seed.csv
│   └── gas_upload_template.csv
├── src/
│   ├── analytics.py          # existing FPL/electric analytics
│   ├── loader.py             # existing FPL workbook loader
│   ├── gas_analytics.py      # additive gas + Site EUI analytics
│   ├── gas_loader.py         # gas Excel/CSV loader + template generator
│   ├── exporter.py
│   ├── auth.py
│   ├── reference_io.py
│   └── storage.py
└── tests/
    ├── test_analytics.py
    ├── test_demand.py
    ├── test_loader.py
    └── test_gas.py
```

## Run locally

Python 3.12 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate       # macOS/Linux
# .venv\Scripts\activate        # Windows

pip install -r requirements.txt
streamlit run app.py
```

## Password setup

The entire app requires `APP_PASSWORD`.

For local use, copy:

```text
.streamlit/secrets.toml.example
```

to:

```text
.streamlit/secrets.toml
```

and set:

```toml
APP_PASSWORD = "your-strong-password"
```

Never commit the real `secrets.toml` file.

For Streamlit Community Cloud, set secrets in **App settings → Secrets**.

## Persistent database

Locally the app uses:

```text
data/dashboard.db
```

For a hosted Streamlit deployment, use hosted Postgres so reference edits, demand history and gas history survive redeploys:

```toml
DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require"
```

The startup migration is additive: existing buildings, meters, FPL behavior and demand history are not deleted. Missing `gas_accounts` and `gas_history` tables are created automatically.

## Monthly workflow

1. Run the FPL collector and create the enhanced FPL workbook.
2. Open the dashboard and upload the FPL workbook exactly as before.
3. Keep one cumulative gas-history workbook and append the new month using **Billing Month, Utility Account, Cost, and Therms**.
4. Upload that same cumulative gas-history workbook. Existing month/account rows are updated and new rows are inserted.
5. Review **Natural Gas** for therms/cost/provider analytics.
6. Review **Total Site EUI** for combined building energy intensity.
7. Review **Data Quality** for new FPL meters and new/unmapped gas accounts.
8. Add new buildings, meters or gas accounts under **Reference Data**.

The FPL workbook itself is processed in memory as before. Demand history and gas history are persisted in the database.

## Gas upload format

The gas upload is one cumulative history file. Append new monthly rows to the same workbook and re-upload it. The app accepts `.xlsx` or `.csv`.

For Excel, use a sheet named one of:

- `Gas History` (preferred)
- `Gas`
- `Natural Gas`
- `Gas Upload`

If none of those exist, the first sheet is used.

### Required columns — only these four

```text
Billing Month
Utility Account
Cost
Therms
```

The billing month is stored in the workbook itself, so there is no separate gas-month selector in the dashboard.

`Billing Month + Utility Account` is the unique history key. `Utility Account` is the reference-data lookup key. The dashboard automatically pulls the following from **Reference Data → Gas Accounts**:

- provider (TECO, Gas South, etc.);
- building;
- ERAU account code;
- gas meter;
- rate schedule;
- physical usage group;
- cost component;
- end use (building, generator, etc.);
- whether the account is authoritative for therms;
- whether the gas is included in Site EUI.

If an uploaded Utility Account is not registered, the gas import stops and lists the missing account number(s). Add them under **Reference Data → Gas Accounts**, then upload the same history workbook again. This prevents the app from guessing a building or provider.

A blank template is available from the dashboard Downloads page and in `data/gas_upload_template.xlsx`. The repo also includes `data/ERAU_Gas_History.xlsx`, preloaded with the supplied FY25/26 history, as the recommended file to keep and append each month.

## Gas reference accounts

`data/gas_accounts_seed.csv` contains the TECO and Gas South provider accounts supplied for the Daytona Beach campus. Each row can define:

- provider;
- utility account;
- ERAU account code;
- building;
- gas meter;
- rate schedule;
- usage group;
- cost component;
- end use;
- whether the provider is authoritative for therms;
- whether the gas should be included in Site EUI.

You can change these in **Reference Data → Gas Accounts** without editing Python.

### Add a new gas account

1. Add the building first if it does not exist.
2. Add the physical gas meter under **Reference Data → Meters** if desired.
3. Add the provider account under **Reference Data → Gas Accounts**.
4. If a TECO and Gas South bill represent the same physical usage, give both rows the same `usage_group` and mark only one provider as authoritative for therms.

## Existing building/meter editing

The existing functionality remains available:

- add/edit buildings and floor area;
- add/edit current/historical/shared electric meters;
- add/edit gas meters;
- assign shared-meter allocation fractions;
- resolve newly detected FPL replacement meters.

## Demand analytics

The existing demand module remains intact. It reads demand-bearing FPL PDF details, persists demand history, calculates billed peak kW and W/sf, and distinguishes exact versus estimated demand-charge dollars.

For multi-meter buildings, the displayed building peak is the **sum of each meter's billed peak**, not necessarily a coincident interval peak.

## Data-quality safeguards

### Electricity

- unmapped electric meter → excluded and flagged;
- unresolved shared meter → excluded until allocations total 100%;
- missing floor area → EUI blank;
- historical/replacement meters remain usable;
- meter suggestions are advisory until saved.

### Natural gas

- unmapped provider account → flagged;
- unregistered provider row → flagged even if upload building ID temporarily maps it;
- TECO/Gas South shared usage group → therms de-duplicated, costs summed;
- generator/excluded account → therms visible but omitted from default Gas/Site EUI;
- missing floor area → gas cost/use visible, Gas EUI blank;
- zero therms with positive cost → shown as a fixed-charge exception.

## Downloads

With an FPL workbook loaded, the analyzed Excel download contains all existing FPL sheets plus additive gas sheets when gas history exists:

- Gas History
- Gas Enriched
- Gas Usage Groups
- Gas Building Monthly
- Gas Snapshot
- Gas Campus Monthly
- Gas Providers
- Gas Unmapped
- Gas Fixed Charges
- Gas Accounts Ref
- Total Site EUI

Standalone CSV downloads are also available for gas history and the gas building snapshot.

## Deploy to Streamlit Community Cloud

1. Push the repository to GitHub.
2. Create the Streamlit app using `app.py` as the entry point.
3. Set `APP_PASSWORD` in Secrets.
4. Set `DATABASE_URL` to hosted Postgres for persistence.
5. Optionally set `ADMIN_PASSWORD` to protect reference editing with a second password.
6. Deploy/reboot the app after the first schema-changing update.

## Tests

```bash
pytest -q
```

The test suite covers the existing FPL/electric behavior plus gas loading, gas persistence, TECO/Gas South therm de-duplication, generator EUI exclusion and combined Site EUI math.

## September 2026 FPL combined-history update

The dashboard now accepts any of these FPL workbook layouts:

- `FPL Bills Combined` (preferred cumulative workbook)
- `Bill History` (legacy enhanced collector)
- `Selected Enriched` (monthly full-extraction export)

Electric bill history is now persisted in the dashboard database, using `accountNumber + dateBilled` as the upsert key. This means you can initialize the database with `FPL_Combined_Bills.xlsx`, then upload future monthly workbooks without losing prior electric history.

Demand history remains persisted separately and is extracted directly from `FPL Bills Combined` or `Selected Enriched` when those sheets are present.

The Demand page now adds:

- latest-month demand-account and demand-meter counts
- demand cost composition detail
- average demand and maximum-demand $/kW by rate schedule
- a warning/review table when measured demand kW is present but the demand $/kW rate is missing

Account numbers are normalized as 10-digit text where possible to reduce Excel scientific-notation issues.

## FPL account-to-building mapping

Electricity analytics now use two mapping paths:

1. Meter ID mapping when a current meter reference exists.
2. FPL account-number mapping from `data/electric_accounts_seed.csv` as the authoritative fallback.

This prevents building analytics from disappearing when meter identifiers change or are not yet registered. The account mapping also carries the supplied GSF values used for EUI where available.
