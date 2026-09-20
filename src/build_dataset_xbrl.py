"""
Recorre los 146 archivos XBRL (5 empresas x años x trimestres) y arma
raw_quarterly.csv con el mismo esquema que usábamos con los PDF.
"""
import os
import re
import warnings
import pandas as pd

warnings.filterwarnings("ignore")
import extract_xbrl as x

BASE_DIR = os.path.join(os.path.dirname(__file__), "..", "data_xbrl")


def identificar_trimestre(nombre_archivo):
    nombre = nombre_archivo.lower()
    if "cierre" in nombre:
        return 4
    m = re.search(r"q([1-3])", nombre)
    return int(m.group(1)) if m else None


def main():
    filas = []
    for empresa in sorted(os.listdir(BASE_DIR)):
        carpeta_empresa = os.path.join(BASE_DIR, empresa)
        if not os.path.isdir(carpeta_empresa):
            continue
        for año in sorted(os.listdir(carpeta_empresa)):
            carpeta_año = os.path.join(carpeta_empresa, año)
            if not os.path.isdir(carpeta_año):
                continue
            for archivo in sorted(os.listdir(carpeta_año)):
                if not archivo.lower().endswith(".xbrl"):
                    continue
                trimestre = identificar_trimestre(archivo)
                if trimestre is None:
                    print(f"AVISO: no se pudo identificar el trimestre de {empresa}/{año}/{archivo}")
                    continue
                ruta = os.path.join(carpeta_año, archivo)
                print(f"Procesando: {empresa}/{año}/{archivo}")
                try:
                    fila = x.procesar_archivo_xbrl(ruta, empresa, int(año), trimestre)
                except Exception as e:
                    print(f"  ERROR: {e}")
                    fila = {"empresa": empresa, "año": int(año), "trimestre": trimestre,
                            "archivo": archivo, "error": str(e)}
                filas.append(fila)

    df = pd.DataFrame(filas)
    df = df.sort_values(["empresa", "año", "trimestre"]).reset_index(drop=True)
    ruta_salida = os.path.join(os.path.dirname(__file__), "..", "data_xbrl_out")
    os.makedirs(ruta_salida, exist_ok=True)
    ruta_csv = os.path.join(ruta_salida, "raw_quarterly_xbrl.csv")
    df.to_csv(ruta_csv, index=False)
    print(f"\nListo: {len(df)} filas guardadas en {ruta_csv}")
    return df


if __name__ == "__main__":
    main()
