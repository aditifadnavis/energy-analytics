"""
Multi-Store Energy, Water & Solar Analytics  (simulated data)
Author: Aditi Fadnavis
Covers: data validation, anomaly detection, benchmarking (EUI), carbon, forecasting,
        solar PV performance ratio, savings quantification.
Run:  python energy_analytics.py
"""
import numpy as np, pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

OUT = Path("outputs"); OUT.mkdir(exist_ok=True)
rng = np.random.default_rng(42)

# ---- Assumptions (update with real values; sources noted) ----
GRID_EF = 0.71        # kg CO2e / kWh  (approx. India grid factor; check latest CEA CO2 Baseline Database)
DIESEL_EF = 2.68      # kg CO2e / litre diesel
TARIFF = 8.0          # INR / kWh (assumed commercial tariff)
WATER_TARIFF = 40.0   # INR / kL (assumed)
DIESEL_PRICE = 90.0   # INR / litre (assumed)

# ---------------- 1. Simulate store data ----------------
stores = pd.DataFrame({
    "store": ["Store A","Store B","Store C","Store D","Store E"],
    "area_m2": [8000, 12000, 6000, 10000, 9000],
    "solar_kWp": [0, 250, 0, 150, 0],
})
months = pd.date_range("2024-01-01", "2025-12-01", freq="MS")
temp = 27 + 9*np.sin((months.month.values-3)/12*2*np.pi)       # city temp proxy (degC)
rows = []
for _, s in stores.iterrows():
    base_int = rng.uniform(11, 15)                               # kWh/m2/month baseline
    for i, m in enumerate(months):
        cooling = max(temp[i]-24, 0)*0.25
        kwh = s.area_m2*(base_int + cooling)/1.0*rng.normal(1, 0.025)
        water = s.area_m2*0.028*rng.normal(1, 0.04)               # kL
        diesel = rng.normal(180, 25)                              # litres (DG backup)
        irr = 5.2 + 1.2*np.sin((m.month-3)/12*2*np.pi)            # kWh/m2/day
        solar = s.solar_kWp*irr*m.days_in_month*0.78*rng.normal(1, 0.03) if s.solar_kWp else 0
        rows.append(dict(month=m, store=s.store, area_m2=s.area_m2, temp_c=temp[i], kwh_grid=kwh,
                         water_kl=water, diesel_l=diesel, irradiation=irr, solar_kwh=solar, solar_kWp=s.solar_kWp))
df = pd.DataFrame(rows)
# inject known issues (so detection can be tested)
df.loc[(df.store=="Store C")&(df.month>="2025-06-01")&(df.month<="2025-09-01"), "kwh_grid"] *= 1.28   # HVAC left on / night baseload
df.loc[(df.store=="Store B")&(df.month=="2025-03-01"), "water_kl"] *= 2.4                              # leak
df.loc[(df.store=="Store E")&(df.month=="2025-08-01"), "diesel_l"] *= 4                               # abnormal DG run
df.loc[(df.store=="Store D")&(df.month>="2025-07-01"), "solar_kwh"] *= 0.7                              # soiling/inverter fault
df.loc[3, "kwh_grid"] = np.nan                                                                          # missing reading
df.loc[10, "water_kl"] = -5                                                                             # invalid reading

# ---------------- 2. Data validation ----------------
issues = []
for col in ["kwh_grid","water_kl","diesel_l","solar_kwh"]:
    for idx in df.index[df[col].isna()]:
        issues.append((df.at[idx,"store"], df.at[idx,"month"].date(), col, "missing"))
    for idx in df.index[df[col] < 0]:
        issues.append((df.at[idx,"store"], df.at[idx,"month"].date(), col, "negative"))
val = pd.DataFrame(issues, columns=["store","month","field","issue"])
for col in ["kwh_grid","water_kl"]:                       # fix: interpolate within store
    df.loc[df[col] < 0, col] = np.nan
    df[col] = df.groupby("store")[col].transform(lambda x: x.interpolate().bfill().ffill())

# ---------------- 3. KPIs: EUI, carbon, water intensity ----------------
df["total_kwh"] = df.kwh_grid + df.solar_kwh
df["eui_kwh_m2"] = df.total_kwh/df.area_m2
df["co2_t"] = (df.kwh_grid*GRID_EF + df.diesel_l*DIESEL_EF)/1000
df["water_l_m2"] = df.water_kl*1000/df.area_m2
bench = (df.groupby("store").agg(avg_EUI=("eui_kwh_m2","mean"), avg_water_l_m2=("water_l_m2","mean"),
         total_CO2_t=("co2_t","sum")).round(2))
bench["EUI_vs_portfolio_%"] = ((bench.avg_EUI/bench.avg_EUI.mean()-1)*100).round(1)

# ---------------- 4. Anomaly detection (weather-normalised) ----------------
def anomalies(frame, col, thresh=3.0):
    out = []
    for st, g in frame.groupby("store"):
        X = np.column_stack([np.ones(len(g)), np.maximum(g.temp_c-24, 0)]) if col=="kwh_grid" else np.ones((len(g),1))
        y = g[col].values
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        res = y - X@beta
        mad = np.median(np.abs(res-np.median(res)))*1.4826
        z = res/mad if mad else res*0
        for (idx, zz, r) in zip(g.index, z, res):
            if abs(zz) > thresh: out.append(dict(store=st, month=frame.at[idx,"month"].date(), metric=col,
                                    actual=round(frame.at[idx,col],1), expected=round(frame.at[idx,col]-r,1), z_score=round(zz,1)))
    return out
an = pd.DataFrame(anomalies(df,"kwh_grid")+anomalies(df,"water_kl")+anomalies(df,"diesel_l"))
tariff = {"kwh_grid":TARIFF,"water_kl":WATER_TARIFF,"diesel_l":DIESEL_PRICE}
an["excess"] = (an.actual-an.expected).round(1)
an["excess_cost_INR"] = (an.excess*an.metric.map(tariff)).round(0)

# ---------------- 5. Solar performance ratio ----------------
sol = df[df.solar_kWp>0].copy()
sol["days"] = sol.month.dt.days_in_month
sol["expected_kwh"] = sol.solar_kWp*sol.irradiation*sol.days
sol["PR"] = sol.solar_kwh/sol.expected_kwh
sol["flag"] = np.where(sol.PR < 0.70, "LOW PR - inspect", "ok")
solar_flags = sol[sol.flag!="ok"][["store","month","PR","flag"]].assign(PR=lambda x: x.PR.round(2))

# ---------------- 6. Forecast next 6 months (trend + seasonality) ----------------
def forecast(g, col, n=6):
    t = np.arange(len(g)); m = g.month.dt.month.values
    X = np.column_stack([np.ones(len(g)), t] + [(m==k).astype(float) for k in range(2,13)])
    beta = np.linalg.lstsq(X, g[col].values, rcond=None)[0]
    fut = pd.date_range(g.month.max()+pd.offsets.MonthBegin(1), periods=n, freq="MS")
    tf = np.arange(len(g), len(g)+n); mf = fut.month.values
    Xf = np.column_stack([np.ones(n), tf] + [(mf==k).astype(float) for k in range(2,13)])
    return pd.DataFrame({"month":fut, "forecast_kwh":(Xf@beta).round(0)})
# forecast on cleaned baseline (exclude flagged anomaly months)
fc = []
for st,g in df.groupby("store"):
    bad = set(an[(an.store==st)&(an.metric=="kwh_grid")].month)
    f = forecast(g[~g.month.dt.date.isin(bad)], "kwh_grid"); f["store"]=st; fc.append(f)
fc = pd.concat(fc)

# ---------------- 7. Savings summary ----------------
saving = an[an.excess>0].groupby("metric").agg(events=("excess","size"), excess_units=("excess","sum"), excess_cost_INR=("excess_cost_INR","sum")).round(0)

# ---------------- 8. Charts ----------------
fig, ax = plt.subplots(figsize=(9,4))
g = df[df.store=="Store C"]; ax.plot(g.month, g.kwh_grid/1000, marker="o", label="Actual")
a = an[(an.store=="Store C")&(an.metric=="kwh_grid")]
ax.scatter(pd.to_datetime(a.month), a.actual/1000, color="red", zorder=5, label="Flagged anomaly")
ax.set_title("Store C - Grid electricity (MWh/month) with anomalies"); ax.legend(); fig.tight_layout(); fig.savefig(OUT/"store_c_anomaly.png", dpi=130); plt.close()
fig, ax = plt.subplots(figsize=(7,4)); bench.avg_EUI.plot.bar(ax=ax, color="#1F4E3D")
ax.axhline(bench.avg_EUI.mean(), color="orange", ls="--", label="Portfolio mean"); ax.set_ylabel("kWh/m2/month"); ax.set_title("EUI benchmark by store"); ax.legend(); fig.tight_layout(); fig.savefig(OUT/"eui_benchmark.png", dpi=130); plt.close()
fig, ax = plt.subplots(figsize=(9,4))
for st,g in sol.groupby("store"): ax.plot(g.month, g.PR, marker="o", label=st)
ax.axhline(0.70, color="red", ls="--", label="PR alert threshold"); ax.set_title("Solar PV performance ratio"); ax.legend(); fig.tight_layout(); fig.savefig(OUT/"solar_pr.png", dpi=130); plt.close()

# ---------------- 9. Excel report ----------------
with pd.ExcelWriter(OUT/"energy_report.xlsx") as w:
    bench.to_excel(w,sheet_name="EUI_Benchmark"); an.to_excel(w,sheet_name="Anomalies",index=False); saving.to_excel(w,sheet_name="Savings_Summary")
    solar_flags.to_excel(w,sheet_name="Solar_Alerts",index=False); fc.to_excel(w,sheet_name="Forecast",index=False)
    val.to_excel(w,sheet_name="Data_Validation_Log",index=False); df.assign(month=df.month.dt.date).round(2).to_excel(w,sheet_name="Clean_Data",index=False)
print("== EUI benchmark ==\n",bench,"\n\n== Anomalies ==\n",an.to_string(index=False),"\n\n== Savings ==\n",saving,
      "\n\n== Solar alerts ==\n",solar_flags.to_string(index=False),"\n\n== Validation log ==\n",val.to_string(index=False))
