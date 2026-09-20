"""
Extrae datos financieros de archivos XBRL usando Arelle (arelle.Cntlr), siguiendo
la misma metodología del notebook de referencia (NETINCOME_STOCKPRICE_FINAL.ipynb):
cargar el XBRL, convertir sus "facts" en una tabla, y filtrar por nombre de
concepto + fechas del contexto.

Dos diferencias respecto al notebook de referencia, necesarias para nuestro caso
(Bancolombia no las necesitaba):

1. `skipDTS=True`: nuestro entorno no tiene salida a internet hacia
   superfinanciera.gov.co ni xbrl.org, que es de donde Arelle normalmente
   descarga el esquema/taxonomía (DTS) para resolver cada concepto. Sin esa
   descarga, `fact.concept` queda vacío -pero `fact.qname` sí funciona
   igual de bien-, así que usamos ese en su lugar.

2. Filtro de "sin dimensión" (`fact.context.qnameDims`): los emisores de este
   proyecto tienen desgloses por segmento/negocio dentro del mismo XBRL (a
   diferencia de Bancolombia), así que hay que asegurarse de tomar el hecho
   SIN dimensión (el total real), no uno de los desgloses.
"""
import os
import re
import warnings
import datetime
import pandas as pd

warnings.filterwarnings("ignore")
from arelle import Cntlr

# --- Conceptos de la taxonomía IFRS que necesitamos, con variantes por si
#     alguna empresa usa la extensión local (-co) en vez del estándar ---
CONCEPTOS_BALANCE = {
    "efectivo":            ["ifrs:CashAndCashEquivalents"],
    "activo_corriente":    ["ifrs:CurrentAssets", "ifrs-co:CurrentAssets"],
    "activo_total":        ["ifrs:Assets"],
    "pasivo_corriente":    ["ifrs:CurrentLiabilities", "ifrs-co:CurrentLiabilities"],
    "pasivo_no_corriente": ["ifrs:NoncurrentLiabilities", "ifrs-co:NoncurrentLiabilities"],
    "pasivo_total":        ["ifrs:Liabilities"],
    "patrimonio_total":    ["ifrs:Equity"],
    "ganancias_retenidas": ["ifrs:RetainedEarnings"],
    "inventarios":         ["ifrs:Inventories"],
    "deuda_cp":            ["co-sfc-core:ObligacionesFinancierasCorrientes"],
    "deuda_lp":            ["co-sfc-core:ObligacionesFinancierasNoCorrientes"],
    "deuda_cp_amplia":     ["ifrs:CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",
                             "ifrs:ShorttermBorrowings"],
    "deuda_lp_amplia":     ["ifrs:LongtermBorrowings"],
    "bonos_cp":            ["ifrs:CurrentBondsIssuedAndCurrentPortionOfNoncurrentBondsIssued",
                             "ifrs:CurrentNotesAndDebenturesIssuedAndCurrentPortionOfNoncurrentNotesAndDebenturesIssued"],
    "bonos_lp":            ["ifrs:NoncurrentPortionOfNoncurrentBondsIssued",
                             "ifrs:NoncurrentPortionOfNoncurrentNotesAndDebenturesIssued"],
}

CONCEPTOS_RESULTADOS = {
    "ingresos":           ["ifrs:Revenue", "ifrs-co:Revenue"],
    "utilidad_neta":      ["ifrs:ProfitLossAttributableToOwnersOfParent"],
    "utilidad_operativa": ["ifrs:ProfitLossFromOperatingActivities"],
    "gastos_financieros": ["ifrs:FinanceCosts"],
    "impuesto_renta":     ["ifrs:IncomeTaxExpenseContinuingOperations",
                            "ifrs-co:IncomeTaxExpenseContinuingOperations"],
}

FIN_TRIMESTRE = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def detectar_escala(ruta):
    """Detecta la escala real usando el elemento estructurado
    'RoundingUsedInFinancialStatements' que declaran los emisores dentro del
    propio XBRL (mucho más confiable que buscar la frase suelta en cualquier
    parte del texto, como sí había que hacer con los PDF). Devuelve el factor
    para llevar el valor a MILLONES de pesos.

    OJO con el caso 'Pesos' (sin escalar en absoluto): ISAGEN lo declara así,
    y ahí hay que dividir por 1.000.000 para llegar a millones -si no, los
    valores quedan un millón de veces más grandes de lo real-."""
    for enc in ("utf-8", "iso-8859-1"):
        try:
            with open(ruta, encoding=enc) as f:
                texto = f.read()
            break
        except UnicodeDecodeError:
            continue

    m = re.search(r"RoundingUsedInFinancialStatements[^>]*>([^<]*)<", texto, re.IGNORECASE)
    if m:
        declarado = m.group(1).strip().lower()
        if "miles de millones" in declarado:
            return 1000.0
        if "miles" in declarado:
            return 0.001
        if "millones" in declarado:
            return 1.0
        if "pesos" in declarado:  # declarado como 'Pesos' a secas, sin escalar
            return 0.000001

    # Respaldo si no existe el elemento estructurado: buscar la frase en el texto
    if re.search(r"miles de millones", texto, re.IGNORECASE):
        return 1000.0
    if re.search(r"miles de pesos", texto, re.IGNORECASE):
        return 0.001
    if re.search(r"millones de pesos", texto, re.IGNORECASE):
        return 1.0
    return None  # no se encontró declaración explícita -> hay que revisar a mano


def cargar_facts(ruta):
    """Carga un XBRL con Arelle y devuelve sus 'facts' como DataFrame, igual
    que en el notebook de referencia (con skipDTS=True por la razón explicada
    arriba, y usando fact.qname en vez de fact.concept.qname).
    `logFileName='/dev/null'` silencia las advertencias de red que Arelle
    imprime al intentar (sin éxito, por skipDTS) contactar los servidores de
    la taxonomía -son ruido esperado, no un problema real-.

    Además de contar las dimensiones, se guarda si TODAS ellas son miembros
    de tipo 'Total' (ej. 'EntitysTotalForSubsidiariesMember'): en varias notas
    de la taxonomía IFRS, un desglose por categoría (subsidiarias, segmentos,
    partes relacionadas) incluye una fila de total que técnicamente lleva una
    dimensión, pero esa dimensión ES el total consolidado, no un subconjunto
    parcial -hay que tratarla igual que si no tuviera dimensión-."""
    ctrl = Cntlr.Cntlr(logFileName="/dev/null")
    ctrl.modelManager.skipDTS = True
    xbrl = ctrl.modelManager.load(ruta)

    filas = []
    for f in xbrl.facts:
        dims = f.context.qnameDims if f.context is not None else None
        n_dims = len(dims) if dims else 0
        es_todo_total = n_dims > 0 and all(
            "total" in str(getattr(m, "memberQname", m)).lower() for m in dims.values()
        )
        filas.append({
            "nombre": str(f.qname),
            "valor_texto": f.value,
            "contextID": f.contextID,
            "es_instante": f.context.isInstantPeriod if f.context is not None else None,
            "es_duracion": f.context.isStartEndPeriod if f.context is not None else None,
            "inicio": f.context.startDatetime if f.context is not None else None,
            "fin": f.context.endDatetime if f.context is not None else None,
            "num_dimensiones": n_dims,
            "es_total_efectivo": (n_dims == 0) or es_todo_total,
        })
    df = pd.DataFrame(filas)
    df["valor"] = pd.to_numeric(df["valor_texto"], errors="coerce")
    return df


def buscar_concepto_balance(facts, conceptos, fecha_cierre):
    """Busca un concepto de balance (instantáneo), en la fecha de cierre
    exacta del periodo. Se prefiere ESTRICTAMENTE el hecho sin ninguna
    dimensión cuando existe; solo se usa un hecho marcado como 'total
    efectivo' por dimensión (ver cargar_facts) si no hay ningún candidato
    sin dimensión -para partidas de balance como pasivo corriente, se
    comprobó con CELSIA que el 'total para subsidiarias' de una nota de
    desglose puede NO coincidir con el total consolidado real de esa
    partida puntual, así que no es seguro tratarlo como equivalente por
    defecto aquí (a diferencia de los conceptos de resultados, donde sí se
    ha visto que coincide)-."""
    for concepto in conceptos:
        sub_directo = facts[(facts["nombre"] == concepto) &
                             (facts["es_instante"] == True) &
                             (facts["num_dimensiones"] == 0) &
                             (facts["fin"] == fecha_cierre) &
                             (facts["valor"].notna())]
        candidatos = [v for v in sub_directo["valor"].tolist() if v != 0]
        if candidatos:
            return max(candidatos, key=abs)
        # sin candidato directo (sin dimensión) -> probar 'total efectivo' por dimensión
        sub_total = facts[(facts["nombre"] == concepto) &
                           (facts["es_instante"] == True) &
                           (facts["es_total_efectivo"]) &
                           (facts["num_dimensiones"] > 0) &
                           (facts["fin"] == fecha_cierre) &
                           (facts["valor"].notna())]
        candidatos = [v for v in sub_total["valor"].tolist() if v != 0]
        if candidatos:
            return max(candidatos, key=abs)
    return None


def buscar_concepto_resultados(facts, conceptos, fecha_inicio, fecha_cierre):
    """Igual que buscar_concepto_balance, pero para conceptos de resultados
    (acumulado del año, entre el 1 de enero y la fecha de cierre)."""
    for concepto in conceptos:
        sub = facts[(facts["nombre"] == concepto) &
                    (facts["es_duracion"] == True) &
                    (facts["es_total_efectivo"]) &
                    (facts["inicio"] == fecha_inicio) &
                    (facts["fin"] == fecha_cierre) &
                    (facts["valor"].notna())]
        candidatos = sub["valor"].tolist()
        if not candidatos:
            continue
        no_cero = [v for v in candidatos if v != 0]
        if no_cero:
            return max(no_cero, key=abs)
    return None


def resolver_deuda(fila, plazo):
    """Decide el valor final de deuda de corto o largo plazo, cuando existen
    hasta 3 representaciones posibles del mismo dato en el XBRL:

    - `directa`: la etiqueta local de "Obligaciones financieras" (a veces ya
      incluye los bonos, a veces no, según la empresa).
    - `bonos`: los bonos reportados aparte (cuando existen).
    - `amplia`: el concepto genérico de IFRS "Borrowings", que en teoría ya
      trae todo combinado.

    Se compara `directa` contra `amplia` para saber si `directa` ya incluye
    los bonos (si coinciden, sumar `bonos` los contaría dos veces) o si de
    verdad hace falta sumarlos (si `directa + bonos` sí reconstruye `amplia`).
    Si ninguna de las dos coincide, se usa la más grande, salvo que `amplia`
    sea diez veces más grande sin que los bonos expliquen la diferencia -ahí
    se asume que es una etiqueta con la escala mal representada, y se ignora."""
    directa = fila.get(f"deuda_{plazo}")
    bonos = fila.get(f"bonos_{plazo}")
    amplia = fila.get(f"deuda_{plazo}_amplia")
    combinada = (directa or 0) + (bonos or 0) if (directa is not None or bonos is not None) else None

    if directa and amplia and abs(amplia - directa) / abs(directa) < 0.01:
        return directa  # 'directa' ya incluye los bonos
    if combinada and amplia and abs(combinada - amplia) / amplia < 0.01:
        return combinada  # los bonos sí había que sumarlos aparte
    if combinada and amplia and amplia > combinada * 10:
        return combinada  # 'amplia' con la escala mal representada -se descarta-
    candidatos = [v for v in (combinada, amplia, directa) if v]
    return max(candidatos) if candidatos else None


def resolver_depreciacion_amortizacion(facts, fecha_inicio, fecha_cierre, escala):
    """La depreciación y amortización combinada no siempre viene en una sola
    etiqueta; se prueban, en orden, la etiqueta combinada, la suma de
    depreciación + amortización por separado, y por último las versiones
    "Adjustments" del flujo de efectivo que usan algunas empresas."""
    combinaciones = [
        (["ifrs:DepreciationAndAmortisationExpense", "ifrs-co:DepreciationAndAmortisationExpense",
          "ifrs:AdjustmentsForDepreciationAndAmortisationExpense"], None),
        (["ifrs:DepreciationExpense"], ["ifrs:AmortisationExpense"]),
        (["ifrs:AdjustmentsForDepreciationAndAmortisationExpenseAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss"], None),
        (["ifrs:AdjustmentsForDepreciationExpense"], ["ifrs:AdjustmentsForAmortisationExpense"]),
    ]
    for conceptos_a, conceptos_b in combinaciones:
        a = buscar_concepto_resultados(facts, conceptos_a, fecha_inicio, fecha_cierre)
        b = buscar_concepto_resultados(facts, conceptos_b, fecha_inicio, fecha_cierre) if conceptos_b else None
        if a is not None or b is not None:
            valor = (a or 0) + (b or 0)
            return valor * escala if escala else valor
    return None


def procesar_archivo_xbrl(ruta, empresa, año, trimestre):
    """Extrae todos los campos de un archivo XBRL. `fecha_cierre` en Arelle
    queda como el día SIGUIENTE al de cierre real (ej. 30 jun -> 2023-07-01
    00:00), por la convención de 'instante como el inicio del día después'
    que usa la librería -por eso se suma 1 día al construir la fecha-."""
    mes, dia = FIN_TRIMESTRE[trimestre]
    fecha_cierre = datetime.datetime(int(año), mes, dia) + datetime.timedelta(days=1)
    fecha_inicio = datetime.datetime(int(año), 1, 1)

    escala = detectar_escala(ruta)
    facts = cargar_facts(ruta)

    fila = {"empresa": empresa, "año": int(año), "trimestre": trimestre,
            "archivo": os.path.basename(ruta), "escala_detectada": escala}

    for campo, conceptos in CONCEPTOS_BALANCE.items():
        valor = buscar_concepto_balance(facts, conceptos, fecha_cierre)
        fila[campo] = valor * escala if (valor is not None and escala) else valor

    for plazo in ("cp", "lp"):
        fila[f"deuda_{plazo}"] = resolver_deuda(fila, plazo)
    for campo in ("deuda_cp_amplia", "deuda_lp_amplia", "bonos_cp", "bonos_lp"):
        fila.pop(campo, None)

    for campo, conceptos in CONCEPTOS_RESULTADOS.items():
        valor = buscar_concepto_resultados(facts, conceptos, fecha_inicio, fecha_cierre)
        fila[campo] = valor * escala if (valor is not None and escala) else valor

    fila["depreciacion_amortizacion"] = resolver_depreciacion_amortizacion(
        facts, fecha_inicio, fecha_cierre, escala)

    for campo in ("gastos_financieros", "impuesto_renta"):
        if fila.get(campo) is not None:
            fila[campo] = -abs(fila[campo])

    return fila
