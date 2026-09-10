import os
import pandas as pd
import numpy as np

def generate_milp_ready_dataset(
    input_path: str = r"d:\Case Comp\Kearney\Dump\NetGravity_US_Demo_Data.xlsx",
    output_path: str = r"d:\Case Comp\Kearney\Dump\NetGravity_US_Demo_Data_MILP.xlsx"
):
    print(f"Reading input workbook from {input_path}...")
    xl = pd.ExcelFile(input_path)
    sheets = xl.sheet_names
    output_writer = pd.ExcelWriter(output_path, engine="openpyxl")

    for sheet_name in sheets:
        df = xl.parse(sheet_name)

        if sheet_name == "Facilities":
            print("  Updating Facilities sheet...")
            # Issue #1: Rename Fixed_Cost -> Fixed_Cost_Per_Year
            if "Fixed_Cost" in df.columns:
                df = df.rename(columns={"Fixed_Cost": "Fixed_Cost_Per_Year"})
            
            # Issue #4: Add Handling_Cost_Per_Unit if missing
            if "Handling_Cost_Per_Unit" not in df.columns:
                # Insert Handling_Cost_Per_Unit = 2.20
                df["Handling_Cost_Per_Unit"] = 2.20

            # Issue #6: Update Status ACTIVE -> EXISTING
            if "Status" in df.columns:
                df["Status"] = df["Status"].replace({"ACTIVE": "EXISTING"})

        elif sheet_name == "Demand_History":
            print("  Updating Demand_History sheet...")
            # Issue #2: Aggregate Demand_History by (Period, Market_ID, Product_ID), drop Channel
            group_cols = ["Period", "Market_ID", "Product_ID"]
            if "Demand_Units" in df.columns:
                df = df.groupby(group_cols, as_index=False)["Demand_Units"].sum()

        elif sheet_name == "Lanes":
            print("  Updating Lanes sheet...")
            # Issue #5: Add Distance_KM alongside Distance_Miles
            if "Distance_Miles" in df.columns and "Distance_KM" not in df.columns:
                distance_km = (df["Distance_Miles"] * 1.609344).round(1)
                # Insert Distance_KM right after Distance_Miles
                idx = df.columns.get_loc("Distance_Miles") + 1
                df.insert(idx, "Distance_KM", distance_km)

        # Write sheet to output
        df.to_excel(output_writer, sheet_name=sheet_name, index=False)

    # Issue #3: Generate Forecast sheet
    print("  Generating new Forecast sheet...")
    df_demand = xl.parse("Demand_History")
    # Aggregate demand history first
    df_demand_agg = df_demand.groupby(["Period", "Market_ID", "Product_ID"], as_index=False)["Demand_Units"].sum()

    forecast_rows = []
    future_periods = [
        ("2025-01", "2024-01"),
        ("2025-02", "2024-02"),
        ("2025-03", "2024-03"),
        ("2025-04", "2024-04"),
        ("2025-05", "2024-05"),
        ("2025-06", "2024-06"),
        ("2025-07", "2024-07"),
        ("2025-08", "2024-08"),
    ]

    for fut_p, hist_p in future_periods:
        sub = df_demand_agg[df_demand_agg["Period"] == hist_p]
        for _, row in sub.iterrows():
            mkt = row["Market_ID"]
            prd = row["Product_ID"]
            base_units = row["Demand_Units"]
            
            # Apply 6% YoY growth factor with slight random variation (+- 1%)
            growth = 1.06
            fcst_units = int(round(base_units * growth))
            p10 = int(round(fcst_units * 0.92))
            p90 = int(round(fcst_units * 1.08))

            forecast_rows.append({
                "Period": fut_p,
                "Market_ID": mkt,
                "Product_ID": prd,
                "Forecast_Units": fcst_units,
                "Forecast_P10": p10,
                "Forecast_P90": p90,
                "Confidence_Level": 0.80,
                "Unit": "Units"
            })

    df_forecast = pd.DataFrame(forecast_rows)
    df_forecast.to_excel(output_writer, sheet_name="Forecast", index=False)

    output_writer.close()
    print(f"Successfully generated MILP-ready dataset at: {output_path}")

if __name__ == "__main__":
    generate_milp_ready_dataset()
