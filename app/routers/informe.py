from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session, selectinload
from ..dependencies import get_db
from ..config import render_template
from .. import models
from datetime import datetime, timedelta
import pandas as pd
import json
from fpdf import FPDF
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from io import BytesIO
import tempfile

router = APIRouter()


def _facturas_mes(db: Session, mes: str) -> pd.DataFrame:
    """Devuelve un DataFrame con las facturas del mes YYYY-MM."""
    inicio = datetime.strptime(mes + "-01", "%Y-%m-%d")
    if inicio.month == 12:
        fin = inicio.replace(year=inicio.year + 1, month=1)
    else:
        fin = inicio.replace(month=inicio.month + 1)
    rows = (
        db.query(models.Factura.fecha, models.Factura.productos, models.Factura.total)
        .filter(models.Factura.fecha >= inicio, models.Factura.fecha < fin)
        .all()
    )
    data = [
        {
            "fecha": r.fecha,
            "productos": json.loads(r.productos),
            "total": r.total,
        }
        for r in rows
    ]
    df = pd.DataFrame(data, columns=["fecha", "productos", "total"])
    return df


def _inventario_semanal(db: Session, mes: str):
    """Estado de inventario semana a semana."""
    inicio = datetime.strptime(mes + "-01", "%Y-%m-%d")
    if inicio.month == 12:
        fin = inicio.replace(year=inicio.year + 1, month=1)
    else:
        fin = inicio.replace(month=inicio.month + 1)
    semanas = []
    actual = inicio
    insumos = (
        db.query(models.Insumo)
        .options(selectinload(models.Insumo.entradas), selectinload(models.Insumo.salidas))
        .all()
    )
    while actual < fin:
        siguiente = actual + timedelta(days=7)
        datos = []
        for ins in insumos:
            entradas = sum(e.cantidad for e in ins.entradas if actual <= e.fecha < siguiente)
            salidas = sum(s.cantidad for s in ins.salidas if actual <= s.fecha < siguiente)
            datos.append({"nombre": ins.nombre, "entrada": entradas, "salida": salidas})
        semanas.append({"inicio": actual.date(), "fin": (siguiente - timedelta(days=1)).date(), "datos": datos})
        actual = siguiente
    return semanas


def _ventas_semanales(df: pd.DataFrame, mes: str):
    """Totales de ventas por semana dentro del mes."""
    if df.empty:
        return []
    inicio = datetime.strptime(mes + "-01", "%Y-%m-%d")
    if inicio.month == 12:
        fin = inicio.replace(year=inicio.year + 1, month=1)
    else:
        fin = inicio.replace(month=inicio.month + 1)
    semanas = []
    actual = inicio
    index = 1
    while actual < fin:
        siguiente = actual + timedelta(days=7)
        total = df[(df["fecha"] >= actual) & (df["fecha"] < siguiente)]["total"].sum()
        semanas.append({"semana": index, "total": float(total)})
        actual = siguiente
        index += 1
    return semanas


@router.get("/informe", response_class=HTMLResponse)
async def informe_financiero(request: Request, mes: str = None, db: Session = Depends(get_db)):
    mes = mes or datetime.now().strftime("%Y-%m")
    df = _facturas_mes(db, mes)
    total_ventas = df["total"].sum() if not df.empty else 0
    utilidad = total_ventas * 0.3
    ventas_por_tipo = {}
    for _, fila in df.iterrows():
        for p in fila["productos"]:
            ventas_por_tipo[p["producto"]] = ventas_por_tipo.get(p["producto"], 0) + p.get("subtotal", 0)
    ventas_top = sorted(ventas_por_tipo.items(), key=lambda x: x[1], reverse=True)[:5]
    inventario = _inventario_semanal(db, mes)
    inventario_totales = [sum(d["entrada"] - d["salida"] for d in sem["datos"]) for sem in inventario]
    ventas_semanales = _ventas_semanales(df, mes)
    return render_template(request, "informe.html", {
        "mes": mes,
        "total_ventas": total_ventas,
        "utilidad": utilidad,
        "ventas_por_tipo": ventas_por_tipo,
        "ventas_top": ventas_top,
        "inventario": inventario,
        "inventario_totales": inventario_totales,
        "ventas_semanales": ventas_semanales
    })


@router.get("/informe/pdf")
def informe_pdf(mes: str, db: Session = Depends(get_db)):
    df = _facturas_mes(db, mes)
    total_ventas = df["total"].sum() if not df.empty else 0
    utilidad = total_ventas * 0.3
    ventas_por_tipo = {}
    for _, fila in df.iterrows():
        for p in fila["productos"]:
            ventas_por_tipo[p["producto"]] = ventas_por_tipo.get(p["producto"], 0) + p.get("subtotal", 0)

    ventas_top = sorted(ventas_por_tipo.items(), key=lambda x: x[1], reverse=True)[:5]
    inventario = _inventario_semanal(db, mes)
    inventario_totales = [sum(d["entrada"] - d["salida"] for d in sem["datos"]) for sem in inventario]
    ventas_semanales = _ventas_semanales(df, mes)

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, f"Informe financiero {mes}", ln=True, align="C")
    pdf.ln(5)
    pdf.set_font("Arial", size=12)
    pdf.cell(0, 10, f"Total ventas: ${total_ventas:,.0f}", ln=True)
    pdf.cell(0, 10, f"Utilidad estimada: ${utilidad:,.0f}", ln=True)
    pdf.ln(5)

    # Gráfico de ventas por tipo de producto
    if ventas_top:
        labels, values = zip(*ventas_top)
        fig, ax = plt.subplots()
        ax.bar(labels, values, color="skyblue")
        ax.set_title("Ventas por tipo de producto")
        ax.set_ylabel("Ventas $")
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        fig.tight_layout()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmpfile:
            fig.savefig(tmpfile.name, format="png")
            plt.close(fig)
            pdf.image(tmpfile.name, x=10, w=190)
        pdf.ln(5)

    # Gráfico de ventas semanales
    if ventas_semanales:
        labels = [f"Semana {v['semana']}" for v in ventas_semanales]
        values = [v['total'] for v in ventas_semanales]
        fig, ax = plt.subplots()
        ax.plot(labels, values, marker='o')
        ax.set_title("Ventas semana a semana")
        ax.set_ylabel("Ventas")
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        fig.tight_layout()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmpfile:
            fig.savefig(tmpfile.name, format="png")
            plt.close(fig)
            pdf.image(tmpfile.name, x=10, w=190)
        pdf.ln(5)

    # Gráfico de inventario
    if inventario_totales:
        labels = [f"Semana {i+1}" for i in range(len(inventario_totales))]
        fig, ax = plt.subplots()
        ax.bar(labels, inventario_totales, color="salmon")
        ax.set_title("Estado del inventario")
        ax.set_ylabel("Entradas - Salidas")
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        fig.tight_layout()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmpfile:
            fig.savefig(tmpfile.name, format="png")
            plt.close(fig)
            pdf.image(tmpfile.name, x=10, w=190)
        pdf.ln(5)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, "Detalle de inventario", ln=True)
    pdf.set_font("Arial", size=11)
    for idx, sem in enumerate(inventario, start=1):
        pdf.cell(0, 8, f"Semana {idx} ({sem['inicio']} - {sem['fin']})", ln=True)
        for item in sem["datos"]:
            pdf.cell(0, 8, f"  - {item['nombre']}: entradas {item['entrada']} / salidas {item['salida']}", ln=True)
        pdf.ln(2)

    pdf_bytes = pdf.output(dest="S").encode("latin-1")
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=informe_{mes}.pdf"})