"""
Igual que compute_ratios.py del pipeline de PDF, pero adaptado al esquema de
raw_quarterly_xbrl.csv: no hay 'ebitda_reportado' (en XBRL no existe una
etiqueta de EBITDA como tal, a diferencia de lo que CELSIA/ISA sí mencionaban
como texto libre en su PDF), así que el EBITDA se reconstruye con el método
indirecto para las 5 empresas por igual -más simple y más consistente entre
empresas que la mezcla que tocó usar con los PDF-.
"""
import os
import numpy as np
import pandas as pd

RUTA_CRUDA = os.path.join(os.path.dirname(__file__), "..", "data_xbrl_out", "raw_quarterly_xbrl.csv")
RUTA_SALIDA = os.path.join(os.path.dirname(__file__), "..", "data_xbrl_out", "ratios_xbrl.csv")

CAMPOS_ACUMULADOS = ["ingresos", "utilidad_neta", "gastos_financieros",
                     "utilidad_operativa", "impuesto_renta", "depreciacion_amortizacion"]

CAMPOS_BALANCE = ["efectivo", "activo_corriente", "activo_total", "pasivo_corriente",
                   "pasivo_no_corriente", "pasivo_total", "patrimonio_total",
                   "ganancias_retenidas", "deuda_cp", "deuda_lp", "inventarios"]


def a_trimestre_aislado(df):
    df = df.sort_values(["empresa", "año", "trimestre"]).reset_index(drop=True)
    for campo in CAMPOS_ACUMULADOS:
        aislado = []
        for _, fila in df.iterrows():
            if fila["trimestre"] == 1 or pd.isna(fila[campo]):
                aislado.append(fila[campo])
                continue
            anterior = df[(df.empresa == fila.empresa) & (df.año == fila.año) &
                           (df.trimestre == fila.trimestre - 1)]
            if len(anterior) == 1 and not pd.isna(anterior.iloc[0][campo]):
                aislado.append(fila[campo] - anterior.iloc[0][campo])
            else:
                aislado.append(np.nan)
        df[f"{campo}_trim"] = aislado
    return df


def a_ltm(df):
    df = df.sort_values(["empresa", "año", "trimestre"]).reset_index(drop=True)
    for campo in CAMPOS_ACUMULADOS:
        col = f"{campo}_trim"
        ltm = []
        for empresa in df["empresa"].unique():
            idx = df.index[df.empresa == empresa]
            serie = df.loc[idx, col]
            roll = serie.rolling(window=4, min_periods=4).sum()
            ltm.extend(zip(idx, roll))
        ltm_dict = dict(ltm)
        df[f"{campo}_ltm"] = df.index.map(ltm_dict)
    return df


def calcular_ebitda(df):
    """EBITDA reconstruido con el método indirecto para las 5 empresas por
    igual (en XBRL no existe una etiqueta de EBITDA explícita como tal):
    EBITDA = Utilidad neta + Impuesto + Gastos financieros netos + D&A."""
    df["ebitda_final_ltm"] = (df["utilidad_neta_ltm"]
                               + df["depreciacion_amortizacion_ltm"].fillna(0)
                               - df["gastos_financieros_ltm"]
                               - df["impuesto_renta_ltm"])
    return df


def marcar_valores_sospechosos(df):
    df = df.sort_values(["empresa", "año", "trimestre"]).reset_index(drop=True)
    columnas_revisar = ["ingresos_trim", "utilidad_neta_trim"]
    banderas = []
    for _, fila in df.iterrows():
        motivos = []
        for col in columnas_revisar:
            actual = fila[col]
            if pd.isna(actual) or actual == 0:
                continue
            mismo_trim_año_anterior = df[(df.empresa == fila.empresa) &
                                          (df.año == fila.año - 1) &
                                          (df.trimestre == fila.trimestre)]
            if len(mismo_trim_año_anterior) != 1:
                continue
            referencia = mismo_trim_año_anterior.iloc[0][col]
            if pd.isna(referencia) or referencia == 0:
                continue
            if abs(actual) > 3 * abs(referencia) or abs(actual) < abs(referencia) / 3:
                motivos.append(col)
        banderas.append("; ".join(motivos))
    df["revisar_manualmente"] = banderas
    return df


def calcular_ratios(df):
    deuda_total = df["deuda_cp"] + df["deuda_lp"]
    ebitda = df["ebitda_final_ltm"]
    gasto_fin = df["gastos_financieros_ltm"].abs()

    df["deuda_bruta_ebitda"] = deuda_total / ebitda
    df["deuda_neta_ebitda"] = (deuda_total - df["efectivo"]) / ebitda
    df["pasivo_activo"] = df["pasivo_total"] / df["activo_total"]
    df["deuda_patrimonio"] = deuda_total / df["patrimonio_total"]
    df["perfil_vencimientos_cp"] = df["deuda_cp"] / deuda_total

    df["cobertura_intereses"] = ebitda / gasto_fin
    ffo = ebitda - gasto_fin - df["impuesto_renta_ltm"].abs()
    df["ffo_intereses"] = ffo / gasto_fin
    df["ffo_deuda"] = ffo / deuda_total

    df["razon_corriente"] = df["activo_corriente"] / df["pasivo_corriente"]
    df["prueba_acida"] = (df["activo_corriente"] - df["inventarios"].fillna(0)) / df["pasivo_corriente"]
    df["caja_activos"] = df["efectivo"] / df["activo_total"]
    df["caja_deuda_cp"] = df["efectivo"] / df["deuda_cp"]

    df["margen_ebitda"] = ebitda / df["ingresos_ltm"]
    df["margen_neto"] = df["utilidad_neta_ltm"] / df["ingresos_ltm"]

    x1 = (df["activo_corriente"] - df["pasivo_corriente"]) / df["activo_total"]
    x2 = df["ganancias_retenidas"] / df["activo_total"]
    ebit_ltm = df["utilidad_operativa_ltm"].fillna(
        ebitda - df["depreciacion_amortizacion_ltm"].fillna(0))
    x3 = ebit_ltm / df["activo_total"]
    x4 = df["patrimonio_total"] / df["pasivo_total"]
    df["zscore_x1"], df["zscore_x2"], df["zscore_x3"], df["zscore_x4"] = x1, x2, x3, x4
    df["zscore"] = 3.25 + 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
    df["zscore_zona"] = pd.cut(df["zscore"], bins=[-np.inf, 1.1, 2.6, np.inf],
                                 labels=["riesgo", "gris", "segura"])
    return df


def main():
    df = pd.read_csv(RUTA_CRUDA)
    df = a_trimestre_aislado(df)
    df = marcar_valores_sospechosos(df)
    df = a_ltm(df)
    df = calcular_ebitda(df)
    df = calcular_ratios(df)
    df.to_csv(RUTA_SALIDA, index=False)
    print(f"Listo: {len(df)} filas guardadas en {RUTA_SALIDA}")

    n_sospechosos = (df["revisar_manualmente"] != "").sum()
    print(f"\nTrimestres marcados para revisión manual: {n_sospechosos} de {len(df)}")
    if n_sospechosos:
        print(df.loc[df["revisar_manualmente"] != "", ["empresa","año","trimestre","archivo","revisar_manualmente"]]
              .to_string(index=False))

    ratios_clave = ["deuda_bruta_ebitda", "cobertura_intereses", "razon_corriente",
                     "margen_ebitda", "zscore"]
    print("\nFilas con el ratio calculado (de", len(df), "trimestres en total):")
    for r in ratios_clave:
        print(f"  {r:22} -> {df[r].notna().sum()}")
    return df


if __name__ == "__main__":
    main()
